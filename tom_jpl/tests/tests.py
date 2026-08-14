from datetime import datetime
from io import StringIO

import requests
from dateutil.tz import tzutc

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, RequestFactory
from django.template.loader import render_to_string
from django.contrib.auth.models import AnonymousUser
from unittest import mock

from tom_jpl.jpl import ScoutDataService, ScoutDetail
from tom_jpl.management.commands.updatescout import Command, _fetch_mpc_prev_designations
from tom_jpl.models import ScoutDetailHistory
from tom_jpl.tests.factories import ScoutDetailFactory
from tom_targets.models import Target, TargetName


def make_result(overrides=None):
    """Return a minimal valid Scout result dict, with optional field overrides."""
    base = {
        'objectName': 'ZTF10BL',
        'neoScore': 100,
        'neo1kmScore': 0,
        'phaScore': 0,
        'ieoScore': 0,
        'geocentricScore': 1,
        'rating': 2,
        'unc': '1400',
        'uncP1': '1500',
        'caDist': '0.98',
        'arc': '0.35',
        'nObs': 4,
        'rmsN': '0.12',
        'Vmag': '20.8',
        'rate': '1.9',
        'ra': '08:54',
        'dec': '+28',
        'tEphem': '2026-02-11 16:30',
        'lastRun': '2026-02-11 22:45',
    }
    if overrides:
        base.update(overrides)
    return base


def make_result_with_orbits(overrides=None):
    """Return a Scout result that already contains orbit data (per-object query response)."""
    result = make_result(overrides)
    result['orbits'] = {
        'fields': ['idx', 'epoch', 'ec', 'qr', 'tp', 'om', 'w', 'inc', 'H',
                   'dca', 'tca', 'moid', 'vinf', 'geoEcc', 'impFlag'],
        'data': [[0, '2461079.712931752', '8.855752093403347E-01', '4.998861947435592E-01',
                  '2461038.529885748', '1.3898404962804094E+02', '2.6665272638172337E+02',
                  '1.4692644033445657E+01', '25.402927', '4.34144690247134E-03',
                  '2.4610794091831E+06', '1.971147172E-03', '2.790457665E+01',
                  '1.278753791E+03', 0]],
        'count': '1',
    }
    return result


class TestGetFilterThresholds(SimpleTestCase):
    """Tests for ScoutDataService._get_filter_thresholds()"""

    def setUp(self):
        self.ds = ScoutDataService()

    def _set_input_params(self, overrides=None):
        params = {
            'neo_score_min': None,
            'pha_score_min': None,
            'geo_score_max': None,
            'impact_rating_min': None,
            'ca_dist_min': None,
            'pos_unc_min': None,
            'pos_unc_max': None,
        }
        if overrides:
            params.update(overrides)
        self.ds.input_parameters = params

    def test_all_none_returns_permissive_defaults(self):
        """When all optional params are None, defaults should allow everything through."""
        self._set_input_params()
        thresholds = self.ds._get_filter_thresholds()

        self.assertEqual(thresholds['neo_score_min'], 0)
        self.assertEqual(thresholds['pha_score_min'], 0)
        self.assertEqual(thresholds['geo_score_max'], 101)
        self.assertEqual(thresholds['pos_unc_min'], 0)
        self.assertEqual(thresholds['pos_unc_max'], 360 * 60)
        self.assertIsNone(thresholds['impact_rating_min'])
        self.assertIsNone(thresholds['ca_dist_min'])

    def test_explicit_values_are_used(self):
        """Explicitly set values should be returned as-is."""
        self._set_input_params({
            'neo_score_min': 50,
            'pha_score_min': 10,
            'geo_score_max': 3,
            'pos_unc_min': 5,
            'pos_unc_max': 120,
            'impact_rating_min': 1,
            'ca_dist_min': 0.5,
        })
        thresholds = self.ds._get_filter_thresholds()

        self.assertEqual(thresholds['neo_score_min'], 50)
        self.assertEqual(thresholds['pha_score_min'], 10)
        self.assertEqual(thresholds['geo_score_max'], 3)
        self.assertEqual(thresholds['pos_unc_min'], 5)
        self.assertEqual(thresholds['pos_unc_max'], 120)
        self.assertEqual(thresholds['impact_rating_min'], 1)
        self.assertEqual(thresholds['ca_dist_min'], 0.5)

    def test_zero_values_are_preserved_not_replaced_by_default(self):
        """Explicit zero should be kept, not treated as falsy and replaced by a default."""
        self._set_input_params({'neo_score_min': 0, 'pha_score_min': 0, 'pos_unc_min': 0})
        thresholds = self.ds._get_filter_thresholds()

        self.assertEqual(thresholds['neo_score_min'], 0)
        self.assertEqual(thresholds['pha_score_min'], 0)
        self.assertEqual(thresholds['pos_unc_min'], 0)


class TestParseResultValues(SimpleTestCase):
    """Tests for ScoutDataService._parse_result_values()"""

    def setUp(self):
        self.ds = ScoutDataService()

    def test_valid_numeric_strings(self):
        result = make_result({'unc': '1400', 'caDist': '0.98'})
        pos_unc, ca_dist = self.ds._parse_result_values(result)

        self.assertEqual(pos_unc, 1400.0)
        self.assertAlmostEqual(ca_dist, 0.98)

    def test_non_numeric_unc_defaults_to_zero(self):
        """If 'unc' cannot be cast to float, pos_unc should default to 0.0."""
        result = make_result({'unc': 'N/A'})
        pos_unc, _ = self.ds._parse_result_values(result)

        self.assertEqual(pos_unc, 0.0)

    def test_none_ca_dist_returns_none(self):
        """A None 'caDist' (as returned by the API when unknown) should yield None."""
        result = make_result({'caDist': None})
        _, ca_dist = self.ds._parse_result_values(result)

        self.assertIsNone(ca_dist)

    def test_non_numeric_ca_dist_returns_none(self):
        """A non-numeric 'caDist' string that raises TypeError should yield None."""
        result = make_result({'caDist': 'unknown'})
        _, ca_dist = self.ds._parse_result_values(result)

        self.assertIsNone(ca_dist)


class TestPassesFilters(SimpleTestCase):
    """Tests for ScoutDataService._passes_filters()"""

    def setUp(self):
        self.ds = ScoutDataService()
        # Permissive thresholds that let everything through by default
        self.permissive = {
            'neo_score_min': 0,
            'pha_score_min': 0,
            'geo_score_max': 101,
            'impact_rating_min': None,
            'ca_dist_min': None,
            'pos_unc_min': 0,
            'pos_unc_max': 360 * 60,
        }

    def _thresholds(self, overrides=None):
        t = dict(self.permissive)
        if overrides:
            t.update(overrides)
        return t

    def test_passes_with_permissive_thresholds(self):
        result = make_result()
        self.assertTrue(self.ds._passes_filters(result, 1400.0, 0.98, self._thresholds()))

    def test_fails_neo_score_below_minimum(self):
        result = make_result({'neoScore': 30})
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'neo_score_min': 50})))

    def test_fails_pha_score_below_minimum(self):
        result = make_result({'phaScore': 0})
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'pha_score_min': 5})))

    def test_fails_geocentric_score_above_maximum(self):
        result = make_result({'geocentricScore': 6})
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'geo_score_max': 5})))

    def test_passes_geocentric_score_strictly_below_maximum(self):
        result = make_result({'geocentricScore': 4})
        self.assertTrue(self.ds._passes_filters(result, 0.0, None, self._thresholds({'geo_score_max': 5})))

    def test_passes_geocentric_score_of_zero(self):
        result = make_result({'geocentricScore': 0})
        self.assertTrue(self.ds._passes_filters(result, 0.0, None, self._thresholds({'geo_score_max': 5})))

    def test_fails_pos_unc_below_minimum(self):
        result = make_result()
        self.assertFalse(self.ds._passes_filters(result, 50.0, None, self._thresholds({'pos_unc_min': 100})))

    def test_fails_pos_unc_above_maximum(self):
        result = make_result()
        self.assertFalse(self.ds._passes_filters(result, 500.0, None, self._thresholds({'pos_unc_max': 100})))

    def test_passes_when_impact_rating_min_is_none(self):
        """impact_rating_min=None means no impact filter — any rating (or None) should pass."""
        result = make_result({'rating': None})
        self.assertTrue(self.ds._passes_filters(result, 0.0, None, self._thresholds({'impact_rating_min': None})))

    def test_fails_when_rating_is_none_and_impact_rating_min_is_set(self):
        """If a minimum rating is required but the result has no rating, it should be filtered out."""
        result = make_result({'rating': None})
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'impact_rating_min': 1})))

    def test_fails_when_rating_below_minimum(self):
        result = make_result({'rating': 1})
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'impact_rating_min': 2})))

    def test_passes_when_ca_dist_min_is_none(self):
        """ca_dist_min=None means no close-approach filter."""
        result = make_result()
        self.assertTrue(self.ds._passes_filters(result, 0.0, None, self._thresholds({'ca_dist_min': None})))

    def test_fails_when_ca_dist_exceeds_minimum(self):
        """Result should be filtered out when its caDist is greater than the threshold."""
        result = make_result()
        self.assertFalse(self.ds._passes_filters(result, 0.0, 1.5, self._thresholds({'ca_dist_min': 1.0})))

    def test_fails_when_ca_dist_required_but_is_none(self):
        """If a ca_dist_min threshold is set but the result has no caDist, filter it out."""
        result = make_result()
        self.assertFalse(self.ds._passes_filters(result, 0.0, None, self._thresholds({'ca_dist_min': 1.0})))

    def test_passes_when_ca_dist_within_minimum(self):
        result = make_result()
        self.assertTrue(self.ds._passes_filters(result, 0.0, 0.5, self._thresholds({'ca_dist_min': 1.0})))


class TestFetchTargetData(SimpleTestCase):
    """Tests for ScoutDataService._fetch_target_data()"""

    def setUp(self):
        self.ds = ScoutDataService()
        self.query_parameters = {'tdes': ''}

    def test_returns_result_directly_when_orbits_present(self):
        """If the result already has orbit data, no additional query_service call should be made."""
        result = make_result_with_orbits()
        with mock.patch.object(self.ds, 'query_service') as mock_qs:
            target_data = self.ds._fetch_target_data(result, self.query_parameters)

        mock_qs.assert_not_called()
        self.assertEqual(target_data, result)

    def test_fetches_per_object_data_when_orbits_absent(self):
        """If orbits are not present, query_service should be called to fetch per-object data."""
        result = make_result()  # no 'orbits' key
        per_object_result = make_result_with_orbits()

        with mock.patch.object(self.ds, 'query_service', return_value=[per_object_result]) as mock_qs:
            with mock.patch.object(self.ds, 'get_urls', return_value='http://mock-url'):
                target_data = self.ds._fetch_target_data(result, self.query_parameters)

        mock_qs.assert_called_once()
        self.assertEqual(target_data, per_object_result)

    def test_sets_tdes_on_query_parameters_when_fetching(self):
        """query_parameters['tdes'] should be updated to the result's objectName before fetching."""
        result = make_result({'objectName': 'ZTF10BL'})
        per_object_result = make_result_with_orbits()

        with mock.patch.object(self.ds, 'query_service', return_value=[per_object_result]):
            with mock.patch.object(self.ds, 'get_urls', return_value='http://mock-url'):
                self.ds._fetch_target_data(result, self.query_parameters)

        self.assertEqual(self.query_parameters['tdes'], 'ZTF10BL')

    def test_returns_none_when_query_service_returns_none(self):
        """If the per-object query_service call returns None, _fetch_target_data should return None."""
        result = make_result()  # no 'orbits' key

        with mock.patch.object(self.ds, 'query_service', return_value=None):
            with mock.patch.object(self.ds, 'get_urls', return_value='http://mock-url'):
                target_data = self.ds._fetch_target_data(result, self.query_parameters)

        self.assertIsNone(target_data)


class TestQueryTargetsFiltering(TestCase):
    """Integration-level tests for query_targets filtering behaviour using mocked query_service."""

    def setUp(self):
        self.ds = ScoutDataService()
        self.base_input_parameters = {
            'ca_dist_min': None,
            'data_service': 'Scout',
            'geo_score_max': 5,
            'impact_rating_min': None,
            'neo_score_min': None,
            'pha_score_min': None,
            'pos_unc_max': None,
            'pos_unc_min': None,
            'query_name': '',
            'query_save': False,
            'tdes': 'ZTF10BL',
        }

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_returns_empty_list_when_query_service_returns_none(self, mock_qs):
        mock_qs.return_value = None
        targets = self.ds.query_targets(self.base_input_parameters)
        self.assertEqual(targets, [])

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_result_excluded_by_geocentric_score_filter(self, mock_qs):
        """A result with geocentricScore > geo_score_max should be excluded."""
        result = make_result_with_orbits({'geocentricScore': 6})  # fails geo_score_max=5
        mock_qs.return_value = [result]

        targets = self.ds.query_targets(self.base_input_parameters)
        self.assertEqual(targets, [])

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_result_included_when_all_filters_pass(self, mock_qs):
        """A result that satisfies all default filters should be included."""
        result = make_result_with_orbits({'geocentricScore': 1})
        mock_qs.return_value = [result]

        targets = self.ds.query_targets(self.base_input_parameters)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['objectName'], 'ZTF10BL')

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_multiple_results_partial_filter(self, mock_qs):
        """Only results passing all filters should be returned from a multi-result response."""
        passing = make_result_with_orbits({'objectName': 'ZTF10BL', 'geocentricScore': 1})
        failing = make_result_with_orbits({'objectName': 'ZTF99XX', 'geocentricScore': 6})
        mock_qs.return_value = [passing, failing]

        targets = self.ds.query_targets(self.base_input_parameters)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]['objectName'], 'ZTF10BL')


class TestParseRaToDegrees(SimpleTestCase):
    """Tests for ScoutDataService._parse_ra_to_degrees(), backed by astropy.coordinates.Angle."""

    def test_none_returns_none(self):
        self.assertIsNone(ScoutDataService._parse_ra_to_degrees(None))

    def test_hours_minutes(self):
        self.assertAlmostEqual(ScoutDataService._parse_ra_to_degrees('08:54'), 133.5)

    def test_hours_minutes_seconds(self):
        self.assertAlmostEqual(ScoutDataService._parse_ra_to_degrees('08:54:30'), 133.625)

    def test_malformed_string_returns_none(self):
        self.assertIsNone(ScoutDataService._parse_ra_to_degrees('garbage'))

    def test_out_of_range_hours_returns_none(self):
        # Scout should never send this, but a bogus value must be rejected rather than
        # silently producing an RA past 360 degrees.
        self.assertIsNone(ScoutDataService._parse_ra_to_degrees('25:00'))


class TestParseDetailData(SimpleTestCase):
    """Tests for ScoutDataService._parse_detail_data()"""

    def setUp(self):
        self.ds = ScoutDataService()

    def test_returns_reduced_datums_dict_with_expected_keys(self):
        detail_data = make_result_with_orbits()
        reduced_datums = self.ds._parse_detail_data(detail_data)

        self.assertIsInstance(reduced_datums, dict)

        expected_keys = ['num_obs', 'neo_score', 'neo1km_score', 'pha_score', 'ieo_score', 'geocentric_score',
                         'impact_rating', 'ca_dist', 'arc', 'rms', 'uncertainty', 'uncertainty_p1', 'vmag', 'rate',
                         'ra', 'dec', 't_ephem', 'last_run']

        for datum in reduced_datums:
            self.assertTrue(datum in expected_keys)
            # Check that values are not still strings (e.g. '1400') but have been converted to appropriate
            # types (e.g. float 1400.0)
            self.assertNotEqual(str, type(reduced_datums[datum]))

    def test_convert_values(self):
        detail_data = make_result_with_orbits()

        reduced_datums = self.ds._parse_detail_data(detail_data)

        expected_datums = {'num_obs': 4,
                           'neo_score': 100,
                           'neo1km_score': 0,
                           'pha_score': 0,
                           'ieo_score': 0,
                           'geocentric_score': 1,
                           'impact_rating': 2,
                           'ca_dist': 0.98,
                           'arc': 0.35 / 24.0,  # Scout reports arc in hours; stored in days
                           'rms': 0.12,
                           'uncertainty': 1400.0,
                           'uncertainty_p1': 1500.0,
                           'vmag': 20.8,
                           'rate': 1.9,
                           'ra': 133.5,  # '08:54' -> (8 + 54/60) * 15 degrees
                           'dec': 28.0,
                           't_ephem': datetime(2026, 2, 11, 16, 30, tzinfo=tzutc()),
                           'last_run': datetime(2026, 2, 11, 22, 45, tzinfo=tzutc())
                           }

        self.assertDictEqual(reduced_datums, expected_datums)

    def test_convert_values_more_nones(self):
        detail_data = make_result_with_orbits({'rmsN': None, 'unc': None, 'uncP1': None, 'caDist': None,
                                               'lastRun': None, 'arc': None, 'Vmag': None, 'rate': None,
                                               'ra': None, 'dec': None, 'tEphem': None})

        reduced_datums = self.ds._parse_detail_data(detail_data)

        expected_datums = {'num_obs': 4,
                           'neo_score': 100,
                           'neo1km_score': 0,
                           'pha_score': 0,
                           'ieo_score': 0,
                           'geocentric_score': 1,
                           'impact_rating': 2,
                           'ca_dist': None,
                           'arc': None,
                           'rms': None,
                           'uncertainty': None,
                           'uncertainty_p1': None,
                           'vmag': None,
                           'rate': None,
                           'ra': None,
                           'dec': None,
                           't_ephem': None,
                           'last_run': None
                           }

        self.assertDictEqual(reduced_datums, expected_datums)


class TestScoutDataService(TestCase):
    """
    Test the functionality of the JPL ScoutDataService
    """
    def setUp(self):
        self.jpl_ds = ScoutDataService()
        self.input_parameters = {'ca_dist_min': None,
                                 'data_service': 'Scout',
                                 'geo_score_max': 5,
                                 'impact_rating_min': None,
                                 'neo_score_min': None,
                                 'pha_score_min': None,
                                 'pos_unc_max': None,
                                 'pos_unc_min': None,
                                 'query_name': '',
                                 'query_save': False,
                                 'tdes': ''}
        self.scout_results = [{'uncP1': '1500',
                               'tEphem': '2026-02-11 22:45',
                               'caDist': '0.98',
                               'phaScore': 0,
                               'vInf': '20.1',
                               'moid': '0.001',
                               'ra': '08:54',
                               'objectName': 'ZTF10BL',
                               'neo1kmScore': 0,
                               'geocentricScore': 1,
                               'rate': '1.9',
                               'Vmag': '20.8',
                               'dec': '+28',
                               'tisserandScore': 39,
                               'rating': 2,
                               'arc': '0.35',
                               'H': '26.3',
                               'elong': '156',
                               'unc': '1400',
                               'ieoScore': 0,
                               'orbits': {'data': [[0,
                                                    '2461079.712931752',
                                                    '8.855752093403347E-01',
                                                    '4.998861947435592E-01',
                                                    '2461038.529885748',
                                                    '1.3898404962804094E+02',
                                                    '2.6665272638172337E+02',
                                                    '1.4692644033445657E+01',
                                                    '25.402927',
                                                    '4.34144690247134E-03',
                                                    '2.4610794091831E+06',
                                                    '1.971147172E-03',
                                                    '2.790457665E+01',
                                                    '1.278753791E+03',
                                                    0]],
                                          'count': '1',
                                          'fields': ['idx',
                                                     'epoch',
                                                     'ec',
                                                     'qr',
                                                     'tp',
                                                     'om',
                                                     'w',
                                                     'inc',
                                                     'H',
                                                     'dca',
                                                     'tca',
                                                     'moid',
                                                     'vinf',
                                                     'geoEcc',
                                                     'impFlag']},
                               'nObs': 4,
                               'rmsN': '0.12',
                               'signature': {'source': 'NASA/JPL Scout API', 'version': '1.3'},
                               'lastRun': '2026-02-08 13:52',
                               'neoScore': 100},]

        target_params = {'name': 'ZTF10BL',
                         'type': 'NON_SIDEREAL',
                         'permissions': 'OPEN',
                         'scheme': 'MPC_MINOR_PLANET',
                         'epoch_of_elements': 61079.212931752,
                         'mean_anomaly': 4.4452481923884894,
                         'arg_of_perihelion': 266.65272638172337,
                         'eccentricity': 0.8855752093403347,
                         'lng_asc_node': 138.98404962804094,
                         'inclination': 14.692644033445657,
                         'mean_daily_motion': 0.10793879092762414,
                         'semimajor_axis': 4.368687867914702,
                         'epoch_of_perihelion': 61038.029885748,
                         'perihdist': 0.4998861947435592,
                         'abs_mag': 25.402927,
                         'slope': 0.15}
        self.test_target = Target.objects.create(**target_params)
        self.factory = RequestFactory()

    def test_build_query_parameters_no_target(self):
        """
        Test that the build_query_parameters method correctly builds the query parameters for the JPL ScoutDataService
        """
        expected_parameters = {}

        parameters = self.jpl_ds.build_query_parameters(self.input_parameters)

        self.assertEqual(parameters, expected_parameters)
        self.assertEqual(self.jpl_ds.input_parameters, self.input_parameters)

    def test_build_query_parameters_with_target(self):
        """
        Test that the build_query_parameters method correctly builds the query parameters for the JPL ScoutDataService
        """
        self.input_parameters['tdes'] = 'ZTF10BL'
        expected_parameters = {'tdes': 'ZTF10BL', 'orbits': 1, 'n-orbits': 1}
        parameters = self.jpl_ds.build_query_parameters(self.input_parameters)

        self.assertEqual(parameters, expected_parameters)
        self.assertEqual(self.jpl_ds.input_parameters, self.input_parameters)

    def test_build_query_parameters_from_target(self):
        """build_query_parameters_from_target keys the query on the target's Scout designation
        and its output is understood by build_query_parameters, e.g. for re-querying a single
        already-ingested target.
        """
        query_parameters = self.jpl_ds.build_query_parameters_from_target(self.test_target)

        self.assertEqual(query_parameters, {'tdes': 'ZTF10BL'})
        self.assertEqual(
            self.jpl_ds.build_query_parameters(query_parameters),
            {'tdes': 'ZTF10BL', 'orbits': 1, 'n-orbits': 1},
        )

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_query_targets_single(self, mock_client):
        mock_client.side_effect = [self.scout_results, ]
        self.input_parameters['tdes'] = 'ZTF10BL'

        targets = self.jpl_ds.query_targets(self.input_parameters)
        expected_target_results = {'objectName': self.scout_results[0]['objectName'],
                                   'neoScore': self.scout_results[0]['neoScore'],
                                   'phaScore': self.scout_results[0]['phaScore'],
                                   'geocentricScore': self.scout_results[0]['geocentricScore'],
                                   'rating': self.scout_results[0]['rating'],
                                   'unc': self.scout_results[0]['unc'],
                                   'orbits': self.scout_results[0]['orbits'],
                                   }
        for target in targets:
            for key in expected_target_results.keys():
                if key == 'orbits':
                    self.assertEqual(type(target[key]), type(expected_target_results[key]))
                    self.assertEqual(type(target[key]['data']), type(expected_target_results[key]['data']))
                else:
                    self.assertEqual(target[key], expected_target_results[key])

    @mock.patch('tom_jpl.jpl.ScoutDataService.query_service')
    def test_query_targets_filter_on_geo0(self, mock_client):
        first_result = self.scout_results[0].copy()
        first_result['geocentricScore'] = 0  # passes geo_score_max=0 cut
        second_result = self.scout_results[0].copy()
        second_result['objectName'] = 'ZTF99XX'
        mock_client.side_effect = [[first_result, second_result], ]
        self.input_parameters['geo_score_max'] = 0

        targets = self.jpl_ds.query_targets(self.input_parameters)
        self.assertEqual(len(targets), 1)
        expected_target_results = {'objectName': first_result['objectName'],
                                   'neoScore': first_result['neoScore'],
                                   'phaScore': first_result['phaScore'],
                                   'geocentricScore': first_result['geocentricScore'],
                                   'rating': first_result['rating'],
                                   'unc': first_result['unc'],
                                   'orbits': first_result['orbits'],
                                   }
        for target in targets:
            for key in expected_target_results.keys():
                if key == 'orbits':
                    self.assertEqual(type(target[key]), type(expected_target_results[key]))
                    self.assertEqual(type(target[key]['data']), type(expected_target_results[key]['data']))
                else:
                    self.assertEqual(target[key], expected_target_results[key])

    def test_create_target_from_query(self):
        expected_target = self.test_target

        target = self.jpl_ds.create_target_from_query(self.scout_results[0])

        self.assertEqual(target.name, expected_target.name)
        self.assertEqual(target.type, expected_target.type)
        self.assertEqual(target.ra, expected_target.ra)
        self.assertEqual(target.dec, expected_target.dec)
        self.assertEqual(target.scheme, expected_target.scheme)
        self.assertEqual(target.epoch_of_elements, expected_target.epoch_of_elements)
        self.assertAlmostEqual(target.mean_anomaly, expected_target.mean_anomaly, places=6)

        self.assertEqual(Target.objects.count(), 1)

    def test_to_target(self):
        expected_target = self.test_target
        self.test_target.delete()  # delete the existing target to test creation of new one
        request = self.factory.get('/')
        request.user = AnonymousUser()

        with mock.patch('tom_dataservices.dataservices.messages'):
            target_data = self.scout_results[0].copy()
            scout_detail = self.jpl_ds._parse_detail_data(target_data)
            target_data['scout_detail'] = scout_detail
            target = self.jpl_ds.to_target(target_data, request=request)

        self.assertEqual(Target.objects.count(), 1)
        self.assertEqual(target.name, expected_target.name)
        self.assertEqual(target.type, expected_target.type)
        self.assertEqual(target.ra, expected_target.ra)
        self.assertEqual(target.dec, expected_target.dec)
        self.assertEqual(target.scheme, expected_target.scheme)
        self.assertEqual(target.epoch_of_elements, expected_target.epoch_of_elements)
        self.assertAlmostEqual(target.mean_anomaly, expected_target.mean_anomaly, places=6)

        self.assertEqual(ScoutDetail.objects.count(), 1)
        self.assertEqual(target.scout_detail.num_obs, scout_detail['num_obs'])
        self.assertEqual(target.scout_detail.neo_score, scout_detail['neo_score'])
        self.assertEqual(target.scout_detail.pha_score, scout_detail['pha_score'])
        self.assertEqual(target.scout_detail.geocentric_score, scout_detail['geocentric_score'])
        self.assertEqual(target.scout_detail.impact_rating, scout_detail['impact_rating'])
        self.assertEqual(target.scout_detail.ca_dist, scout_detail['ca_dist'])
        self.assertEqual(target.scout_detail.arc, scout_detail['arc'])
        self.assertEqual(target.scout_detail.rms, scout_detail['rms'])
        self.assertEqual(target.scout_detail.uncertainty, scout_detail['uncertainty'])
        self.assertEqual(target.scout_detail.uncertainty_p1, scout_detail['uncertainty_p1'])
        self.assertEqual(target.scout_detail.vmag, scout_detail['vmag'])
        self.assertEqual(target.scout_detail.rate, scout_detail['rate'])
        self.assertEqual(target.scout_detail.ra, scout_detail['ra'])
        self.assertEqual(target.scout_detail.dec, scout_detail['dec'])
        self.assertEqual(target.scout_detail.t_ephem, scout_detail['t_ephem'])
        self.assertEqual(target.scout_detail.last_run, scout_detail['last_run'])

    def test_update_existing_target_from_query(self):
        """Test that create_target_from_query updates an existing Target rather than creating a new one."""
        existing_target = self.test_target
        self.assertEqual(Target.objects.count(), 1)

        with mock.patch('tom_dataservices.dataservices.messages'):
            target_data = self.scout_results[0].copy()
            scout_detail = self.jpl_ds._parse_detail_data(target_data)
            scout_detail['neo_score'] = 50
            target_data['scout_detail'] = scout_detail
            target_data['orbits']['data'][0][2] = .1
            target = self.jpl_ds.to_target(target_data)

        # No new Target should have been created
        self.assertEqual(Target.objects.count(), 1)
        # The existing Target should have been updated with the new data
        self.assertEqual(target.id, existing_target.id)
        self.assertEqual(target.name, existing_target.name)
        self.assertEqual(target.type, existing_target.type)
        self.assertEqual(target.scheme, existing_target.scheme)
        self.assertEqual(target.epoch_of_elements, existing_target.epoch_of_elements)
        self.assertNotAlmostEqual(target.eccentricity, existing_target.eccentricity, places=2)
        self.assertNotAlmostEqual(target.mean_anomaly, existing_target.mean_anomaly, places=2)


class ScoutDetailsPartialTest(TestCase):

    def _render_partial(self, scoutdetail):
        return render_to_string(
            'tom_jpl/partials/scoutdetails_partial.html',
            {'scoutdetail': scoutdetail}
        )

    def test_excludes_none_values(self):
        scoutdetail = ScoutDetailFactory(ieo_score=None)
        rendered = self._render_partial(scoutdetail)
        self.assertNotIn('IEO', rendered)

    def test_excludes_target_and_id(self):
        scoutdetail = ScoutDetailFactory()
        rendered = self._render_partial(scoutdetail)
        self.assertNotIn('target', rendered)
        self.assertNotIn('id"', rendered)  # avoid false positives on e.g. card-id

    def test_displays_neo_score(self):
        scoutdetail = ScoutDetailFactory(neo_score=78)
        rendered = self._render_partial(scoutdetail)
        self.assertIn('NEO', rendered)
        self.assertIn('78', rendered)


# Scout API signature expected by query_service().
_SCOUT_SIGNATURE = {'source': 'NASA/JPL Scout API', 'version': '1.3'}

# A baseline form-parameter set with permissive thresholds (nothing excluded at the
# summary stage), mirroring the defaults produced by ScoutForm.
_PERMISSIVE_INPUT_PARAMETERS = {
    'ca_dist_min': None, 'data_service': 'Scout', 'geo_score_max': 5,
    'impact_rating_min': None, 'neo_score_min': None, 'pha_score_min': None,
    'pos_unc_max': None, 'pos_unc_min': None, 'query_name': '', 'query_save': False,
    'tdes': '',
}


def make_scout_api_get(summary_data, detail_data):
    """Build a ``requests.get`` replacement for the Scout API.

    The summary (list) query is answered with ``summary_data`` (the objects that go
    in the ``data`` array); the per-object query (identified by a ``tdes`` parameter)
    is answered with ``detail_data``. Both responses carry the expected signature.
    """
    summary_payload = {'signature': _SCOUT_SIGNATURE, 'count': len(summary_data), 'data': summary_data}

    def _get(url, data=None, **kwargs):
        if data and data.get('tdes'):
            payload = dict(detail_data, signature=_SCOUT_SIGNATURE)
        else:
            payload = summary_payload
        response = mock.Mock()
        response.json.return_value = payload
        return response

    return _get


class TestScoutIngestionFromMockedApi(TestCase):
    """End-to-end ingest test that mocks the Scout API at the HTTP boundary.

    Patching ``requests.get`` lets the real ``query_service`` -> ``query_targets`` ->
    ``to_target`` chain run (signature check, summary/per-object branching, field
    parsing and the ScoutDetail DB write), guarding the parse seams that the unit
    tests mock away: arc hours -> days, sexagesimal RA -> degrees, and tEphem.
    """

    # Detail overrides that make the parsed candidate satisfy the Rubin ToO Section 2.1
    # filters (rating >= 3, nObs > 5, arc > 1 hr, Vmag > 21.6 North).
    RUBIN_PASSING_OVERRIDES = {'rating': 4, 'nObs': 8, 'arc': '2.0', 'Vmag': '21.9'}

    def setUp(self):
        self.ds = ScoutDataService()
        self.factory = RequestFactory()

    def _ingest(self, summary_obj, detail_obj):
        """Run the full mocked-API ingest and return the created target(s)."""
        fake_get = make_scout_api_get([summary_obj], detail_obj)
        with mock.patch('tom_jpl.jpl.requests.get', side_effect=fake_get):
            targets_data = self.ds.query_targets(dict(_PERMISSIVE_INPUT_PARAMETERS))

        request = self.factory.get('/')
        request.user = AnonymousUser()
        created = []
        with mock.patch('tom_dataservices.dataservices.messages'):
            for target_data in targets_data:
                created.append(self.ds.to_target(target_data, request=request))
        return targets_data, created

    def test_ingests_candidate_and_parses_detail_fields(self):
        summary_obj = make_result(self.RUBIN_PASSING_OVERRIDES)
        detail_obj = make_result_with_orbits(self.RUBIN_PASSING_OVERRIDES)

        targets_data, created = self._ingest(summary_obj, detail_obj)

        self.assertEqual(len(targets_data), 1)
        self.assertEqual(Target.objects.count(), 1)
        self.assertEqual(ScoutDetail.objects.count(), 1)

        sd = created[0].scout_detail
        self.assertEqual(sd.num_obs, 8)
        self.assertAlmostEqual(sd.arc, 2.0 / 24.0)   # Scout reports hours; stored in days
        self.assertAlmostEqual(sd.ra, 133.5)          # '08:54' sexagesimal -> degrees
        self.assertEqual(sd.dec, 28.0)
        self.assertAlmostEqual(sd.vmag, 21.9)
        self.assertAlmostEqual(sd.rate, 1.9)
        self.assertEqual(sd.t_ephem, datetime(2026, 2, 11, 16, 30, tzinfo=tzutc()))

    def test_candidate_excluded_at_summary_stage_creates_nothing(self):
        # A high geocentric score is rejected by the summary filter (geo_score_max=5),
        # so no per-object fetch happens and no Target/ScoutDetail is created.
        summary_obj = make_result(dict(self.RUBIN_PASSING_OVERRIDES, geocentricScore=42))
        detail_obj = make_result_with_orbits(self.RUBIN_PASSING_OVERRIDES)

        targets_data, created = self._ingest(summary_obj, detail_obj)

        self.assertEqual(targets_data, [])
        self.assertEqual(Target.objects.count(), 0)
        self.assertEqual(ScoutDetail.objects.count(), 0)

    def test_history_row_created_on_ingest(self):
        summary_obj = make_result(self.RUBIN_PASSING_OVERRIDES)
        detail_obj = make_result_with_orbits(self.RUBIN_PASSING_OVERRIDES)

        _, created = self._ingest(summary_obj, detail_obj)

        self.assertEqual(ScoutDetailHistory.objects.count(), 1)
        h = created[0].scout_detail_history.get()
        self.assertEqual(h.last_run, datetime(2026, 2, 11, 22, 45, tzinfo=tzutc()))
        self.assertEqual(h.num_obs, 8)
        self.assertAlmostEqual(h.arc, 2.0 / 24.0)
        self.assertAlmostEqual(h.vmag, 21.9)
        # A freshly-ingested candidate is marked active on its current-state row.
        self.assertTrue(created[0].scout_detail.active)

    def test_history_not_duplicated_on_second_ingest_with_same_last_run(self):
        # Ingesting the same Scout recomputation twice must produce exactly one history row.
        summary_obj = make_result(self.RUBIN_PASSING_OVERRIDES)
        detail_obj = make_result_with_orbits(self.RUBIN_PASSING_OVERRIDES)

        self._ingest(summary_obj, detail_obj)
        self._ingest(summary_obj, detail_obj)

        self.assertEqual(ScoutDetailHistory.objects.count(), 1)


class TestUpdateScoutCommand(TestCase):
    """The ``updatescout`` management command reconciles already-ingested Scout candidates.

    Ingestion of *new* candidates now goes through tom_dataservices' ``rundataquery``
    against a saved Scout ``DataServiceQuery``; this command only re-checks candidates
    already marked ``active``, one at a time, via ``build_query_parameters_from_target``.
    Checking each target individually (rather than diffing against one broad query's
    results) means a target that merely stops matching some *other* query's cuts is never
    mistaken for one that has actually left Scout.
    """

    RUBIN_PASSING_OVERRIDES = {'rating': 4, 'nObs': 8, 'arc': '2.0', 'Vmag': '21.9'}

    def _run(self, tdes_response, **opts):
        """Run the command with ``requests.get`` patched to answer any tdes-keyed query.

        ``tdes_response`` is either a detail-object dict (candidate still on Scout) or
        ``None`` (Scout has no record of it any more).
        """
        def fake_get(url, data=None, **kwargs):
            response = mock.Mock()
            if tdes_response is None:
                response.json.return_value = {'error': 'Object not found'}
            else:
                response.json.return_value = dict(tdes_response, signature=_SCOUT_SIGNATURE)
            return response

        out = StringIO()
        with mock.patch('tom_jpl.jpl.requests.get', side_effect=fake_get):
            call_command('updatescout', stdout=out, **opts)
        return out.getvalue()

    def test_still_active_candidate_is_refreshed(self):
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()
        detail_obj = make_result_with_orbits(self.RUBIN_PASSING_OVERRIDES)

        self._run(detail_obj, skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertTrue(scout_detail.active)
        self.assertEqual(scout_detail.num_obs, 8)
        self.assertAlmostEqual(scout_detail.vmag, 21.9)

    def test_departed_candidate_is_marked_inactive(self):
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()

        out = self._run(None, skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertFalse(scout_detail.active)
        self.assertIn('has left Scout', out)

    def test_dry_run_reconcile_writes_nothing(self):
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()

        self._run(None, skip_designations=True, dry_run=True)

        scout_detail.refresh_from_db()
        self.assertTrue(scout_detail.active)

    def test_inactive_candidates_are_not_requeried(self):
        scout_detail = ScoutDetailFactory(active=False)

        with mock.patch('tom_jpl.jpl.requests.get') as mock_get:
            call_command('updatescout', skip_designations=True, stdout=StringIO())

        mock_get.assert_not_called()
        scout_detail.refresh_from_db()
        self.assertFalse(scout_detail.active)

    def test_skip_reconcile_flag_skips_reconciliation(self):
        with mock.patch.object(Command, '_reconcile') as mock_reconcile, \
                mock.patch.object(Command, '_update_designations'):
            call_command('updatescout', skip_reconcile=True, stdout=StringIO())

        mock_reconcile.assert_not_called()

    def test_skip_designations_flag_skips_designation_lookup(self):
        with mock.patch.object(Command, '_reconcile'), \
                mock.patch.object(Command, '_update_designations') as mock_update_designations:
            call_command('updatescout', skip_designations=True, stdout=StringIO())

        mock_update_designations.assert_not_called()


class TestUpdateDesignations(TestCase):
    """``_update_designations`` promotes a Scout target's official IAU designation.

    Regression coverage for the ``related_name='scout_detail'`` reverse-relation lookup,
    plus the rename-on-promotion behaviour: once an object receives an official IAU
    designation, ``Target.name`` becomes that designation (matching community convention)
    and the original Scout provisional is kept as a ``TargetName`` alias. The real MPC
    fetch is mocked out so this doesn't depend on a network call or on the live "Previous
    NEOCP Objects" page containing a matching row.
    """

    def setUp(self):
        self.command = Command(stdout=StringIO())
        self.scout_detail = ScoutDetailFactory()
        self.target = self.scout_detail.target
        self.target.name = 'A11Df9S'
        self.target.save()

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_prev_designations')
    def test_renames_target_and_keeps_provisional_as_alias(self, mock_fetch):
        mock_fetch.return_value = {'A11Df9S': '2026 LX'}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        self.assertTrue(TargetName.objects.filter(name='A11Df9S', target=self.target).exists())

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_prev_designations')
    def test_dry_run_reports_without_writing(self, mock_fetch):
        mock_fetch.return_value = {'A11Df9S': '2026 LX'}

        self.command._update_designations(dry_run=True)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.assertFalse(TargetName.objects.filter(name='A11Df9S').exists())

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_prev_designations')
    def test_ignores_targets_without_scout_detail(self, mock_fetch):
        # A target whose name matches the MPC mapping but was never ingested via Scout.
        other = Target.objects.create(name='A22Eg0T', type=Target.NON_SIDEREAL)
        mock_fetch.return_value = {'A11Df9S': '2026 LX', 'A22Eg0T': '2026 MY'}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        other.refresh_from_db()
        self.assertEqual(other.name, 'A22Eg0T')

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_prev_designations')
    def test_skips_when_designation_already_claimed(self, mock_fetch):
        # Another target already carries the designation the mapping wants to assign here;
        # renaming would collide with Target.name's uniqueness constraint, so it's skipped.
        Target.objects.create(name='2026 LX', type=Target.NON_SIDEREAL)
        mock_fetch.return_value = {'A11Df9S': '2026 LX'}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.assertFalse(TargetName.objects.filter(name='A11Df9S').exists())


class TestFetchMpcPrevDesignations(SimpleTestCase):
    """``_fetch_mpc_prev_designations`` parses the MPC 'Previous NEOCP Objects' HTML table.

    The live MPC page is mocked at the ``requests.get`` boundary so this exercises the
    real HTML-table parsing and row-filtering logic (header rows, dash/None placeholders,
    short rows, empty cells) without a network call.
    """

    # Mirrors the real 5-column NEOCP page (trksub, iau_desig, status, reference, datetime_ut)
    # with representative rows: asteroid designations, a comet (C/...), and lost/dne objects
    # whose iau_desig renders as the literal "None". A <th> header (ignored — the parser only
    # collects <td>) and synthetic edge cases (a <td> header-like row, dash placeholders, a
    # single-cell row, an empty-trksub row) exercise the remaining filter branches.
    SAMPLE_HTML = """
    <table>
      <tr><th>trksub</th><th>iau_desig</th><th>status</th><th>reference</th><th>datetime_ut</th></tr>
      <tr><td>trksub</td><td>iau_desig</td><td>status</td><td>reference</td><td>datetime_ut</td></tr>
      <tr><td>ST26F43</td><td>2026 LW2</td><td>None</td><td>MPEC 2026-L105</td><td>2026-06-15T21:36:25</td></tr>
      <tr><td>P12nn8G</td><td>C/2026 L2</td><td>None</td><td>MPEC 2026-L104</td><td>2026-06-15T20:58:27</td></tr>
      <tr><td>A11DnRU</td><td>2026 LS1</td><td>None</td><td>None</td><td>2026-06-15T15:32:57</td></tr>
      <tr><td>ML81524</td><td>None</td><td>lost</td><td>None</td><td>2026-06-15T11:00:26</td></tr>
      <tr><td>ST26F44</td><td>None</td><td>dne</td><td>None</td><td>2026-06-15T08:44:05</td></tr>
      <tr><td>A33Zz9Q</td><td>&mdash;</td><td>None</td></tr>
      <tr><td>A44Yy8P</td><td>-</td><td>None</td></tr>
      <tr><td>OnlyOneCell</td></tr>
      <tr><td></td><td>2026 ZZ</td></tr>
    </table>
    """

    def _mock_response(self, text):
        response = mock.Mock()
        response.text = text
        return response

    @mock.patch('tom_jpl.management.commands.updatescout.requests.get')
    def test_parses_only_real_designations(self, mock_get):
        mock_get.return_value = self._mock_response(self.SAMPLE_HTML)

        mapping = _fetch_mpc_prev_designations()

        # Asteroid AND comet designations are kept; lost/dne ("None"), dash placeholders,
        # header rows, short rows and empty-trksub rows are all filtered out.
        self.assertEqual(
            mapping,
            {
                'ST26F43': '2026 LW2',
                'P12nn8G': 'C/2026 L2',  # comet designation is aliased like any other
                'A11DnRU': '2026 LS1',
            },
        )
        # lost / dne objects (iau_desig == "None") are excluded.
        self.assertNotIn('ML81524', mapping)
        self.assertNotIn('ST26F44', mapping)

    @mock.patch('tom_jpl.management.commands.updatescout.requests.get')
    def test_empty_table_returns_empty_mapping(self, mock_get):
        mock_get.return_value = self._mock_response('<table></table>')

        self.assertEqual(_fetch_mpc_prev_designations(), {})

    @mock.patch('tom_jpl.management.commands.updatescout.requests.get')
    def test_http_error_propagates(self, mock_get):
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.HTTPError('500')
        mock_get.return_value = response

        with self.assertRaises(requests.HTTPError):
            _fetch_mpc_prev_designations()
