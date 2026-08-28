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
from tom_jpl.management.commands.updatescout import Command, _fetch_mpc_departures, _parse_departures
from tom_jpl.models import ScoutDetailHistory
from tom_jpl.tests.factories import NonSiderealTargetFactory, ScoutDetailFactory, ScoutDetailHistoryFactory
from tom_targets.models import Target, TargetName


def make_departure_row(status='designated', designation=None, reference=None, merged_into=None):
    """Build one value of the normalized outcome dict `_parse_departures` returns."""
    return {'status': status, 'designation': designation, 'reference': reference,
            'merged_into': merged_into}


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

    def test_never_set_input_parameters_returns_permissive_defaults(self):
        """A fresh ScoutDataService (input_parameters never assigned by build_query_parameters,
        e.g. a DataServiceQuery.parameters blob saved/edited without a 'neo_score_min' key) must
        not raise AttributeError/KeyError -- it should behave the same as all-None thresholds.
        """
        thresholds = self.ds._get_filter_thresholds()

        self.assertEqual(thresholds['neo_score_min'], 0)
        self.assertEqual(thresholds['pha_score_min'], 0)
        self.assertEqual(thresholds['geo_score_max'], 101)
        self.assertEqual(thresholds['pos_unc_min'], 0)
        self.assertEqual(thresholds['pos_unc_max'], 360 * 60)
        self.assertIsNone(thresholds['impact_rating_min'])
        self.assertIsNone(thresholds['ca_dist_min'])

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


class TestParseUtcDatetime(SimpleTestCase):
    """Tests for ScoutDataService._parse_utc_datetime(), backed by django.utils.timezone.make_aware."""

    def test_none_returns_none(self):
        self.assertIsNone(ScoutDataService._parse_utc_datetime(None))

    def test_naive_string_is_attached_utc(self):
        # This is the real Scout format: no offset in the string at all.
        result = ScoutDataService._parse_utc_datetime('2026-02-11 16:30')
        self.assertEqual(result, datetime(2026, 2, 11, 16, 30, tzinfo=tzutc()))

    def test_already_offset_string_raises_instead_of_silently_mislabeling(self):
        # Scout has never sent this, but if it ever did, blindly relabeling '16:30-05:00' as
        # '16:30 UTC' (5 hours off from the true instant) must not pass silently.
        with self.assertRaises(ValueError):
            ScoutDataService._parse_utc_datetime('2026-02-11T16:30:00-05:00')


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

    def test_malformed_numeric_fields_return_none_not_raise(self):
        """A present-but-non-numeric value (e.g. a status placeholder instead of a number) must
        degrade to None like a missing value would, not raise and abort the whole ingest/reconcile.
        """
        detail_data = make_result_with_orbits({'rmsN': 'n/a', 'unc': 'n/a', 'uncP1': 'n/a', 'caDist': 'n/a',
                                               'arc': 'n/a', 'Vmag': 'n/a', 'rate': 'n/a', 'dec': 'n/a'})

        reduced_datums = self.ds._parse_detail_data(detail_data)

        for field in ('rms', 'uncertainty', 'uncertainty_p1', 'ca_dist', 'arc', 'vmag', 'rate', 'dec'):
            self.assertIsNone(reduced_datums[field], f'{field} should be None for a malformed input')


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
                               'neoScore': 100}, ]

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

    def test_to_target_does_not_resurrect_inactive_existing_scout_detail(self):
        """A stale target_result (e.g. a cached interactive query result submitted well after
        the query ran) must not silently reactivate a candidate that updatescout's reconciliation
        has already determined has left Scout -- only ScoutDetail *creation* should default
        active=True; updating an existing row must leave active as-is.
        """
        ScoutDetail.objects.create(target=self.test_target, active=False)

        with mock.patch('tom_dataservices.dataservices.messages'):
            target_data = self.scout_results[0].copy()
            target_data['scout_detail'] = self.jpl_ds._parse_detail_data(target_data)
            self.jpl_ds.to_target(target_data)

        self.assertFalse(ScoutDetail.objects.get(target=self.test_target).active)


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


# Fixed values for every field tracked by the history change-detection, so tests can
# create history rows that differ only in the fields they explicitly override.
_TRACKED_FIELD_VALUES = {
    'num_obs': 10, 'neo_score': 85, 'neo1km_score': 5, 'pha_score': 20, 'ieo_score': 0,
    'geocentric_score': 1, 'impact_rating': 1, 'ca_dist': 3.5, 'arc': 2.8, 'rms': 0.42,
    'uncertainty': 25.0, 'uncertainty_p1': 40.0,
}


class ScoutDetailHistoryChangesTest(TestCase):
    """Tests for ScoutDetailHistory.changes_from()."""

    def setUp(self):
        self.target = NonSiderealTargetFactory()

    def _history_row(self, last_run, **overrides):
        values = {**_TRACKED_FIELD_VALUES, **overrides}
        return ScoutDetailHistoryFactory(target=self.target, last_run=last_run, **values)

    def test_no_previous_row_yields_no_changes(self):
        row = self._history_row(datetime(2026, 7, 1, tzinfo=tzutc()))
        self.assertEqual(row.changes_from(None), {})

    def test_detects_changed_tracked_fields(self):
        older = self._history_row(datetime(2026, 7, 1, tzinfo=tzutc()))
        newer = self._history_row(datetime(2026, 7, 2, tzinfo=tzutc()), neo_score=92, rms=0.38)
        self.assertEqual(newer.changes_from(older), {'neo_score': (85, 92), 'rms': (0.42, 0.38)})

    def test_ignores_ephemeris_and_untracked_fields(self):
        older = self._history_row(datetime(2026, 7, 1, tzinfo=tzutc()),
                                  ra=10.0, dec=-5.0, vmag=21.5, rate=1.2,
                                  t_ephem=datetime(2026, 7, 1, tzinfo=tzutc()))
        newer = self._history_row(datetime(2026, 7, 2, tzinfo=tzutc()),
                                  ra=11.0, dec=-6.0, vmag=22.0, rate=2.4,
                                  t_ephem=datetime(2026, 7, 2, tzinfo=tzutc()))
        self.assertEqual(newer.changes_from(older), {})


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
    already marked ``active``. Membership comes from one unconstrained Scout call: the API
    applies no cuts of its own, so the roster it returns is the whole NEOCP and absence from
    it means the object has genuinely left. A per-object call follows only for candidates
    Scout has actually recomputed, because the orbital elements ``to_target`` updates are not
    in a roster row.
    """

    RUBIN_PASSING_OVERRIDES = {'rating': 4, 'nObs': 8, 'arc': '2.0', 'Vmag': '21.9'}

    def _run(self, tdes_response, roster=None, **opts):
        """Run the command with ``requests.get`` patched to answer both Scout call shapes.

        The unconstrained roster call is answered with ``roster``; by default that is a list
        holding just this candidate when it is still on Scout, or one unrelated object when it
        is not -- an *empty* roster means a failed fetch rather than a mass departure, and is
        guarded separately. A tdes-keyed call is answered with ``tdes_response``.
        """
        if roster is None:
            roster = [make_result()] if tdes_response is not None else [make_result({'objectName': 'OTHER01'})]

        def fake_get(url, data=None, **kwargs):
            response = mock.Mock()
            if data and data.get('tdes'):
                if tdes_response is None:
                    response.json.return_value = {'error': 'Object not found'}
                else:
                    response.json.return_value = dict(tdes_response, signature=_SCOUT_SIGNATURE)
            else:
                response.json.return_value = {
                    'signature': _SCOUT_SIGNATURE, 'count': len(roster), 'data': roster,
                }
            return response

        out = StringIO()
        with mock.patch('tom_jpl.jpl.requests.get', side_effect=fake_get):
            call_command('updatescout', stdout=out, **opts)
        return out.getvalue()

    def test_still_active_candidate_is_refreshed(self):
        # last_run=None means we have no stored timestamp, so the roster row always counts as
        # newer and the candidate is refreshed -- from the roster row itself, not a re-fetch.
        scout_detail = ScoutDetailFactory(active=True, last_run=None)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()
        roster = [make_result(self.RUBIN_PASSING_OVERRIDES)]

        out, calls = self._run_counting(roster, skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertTrue(scout_detail.active)
        self.assertEqual(scout_detail.num_obs, 8)
        self.assertAlmostEqual(scout_detail.vmag, 21.9)
        # The listing row carried every stored field, so no per-object call was needed.
        self.assertEqual(len(calls), 1)

    def test_departed_candidate_is_marked_inactive(self):
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()

        out = self._run(None, skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertFalse(scout_detail.active)
        self.assertIn('has left Scout', out)

    def _run_counting(self, roster, tdes_response=None, **opts):
        """Like ``_run`` but also returns how many Scout requests were issued."""
        calls = []

        def fake_get(url, data=None, **kwargs):
            calls.append(dict(data or {}))
            response = mock.Mock()
            if data and data.get('tdes'):
                if tdes_response is None:
                    response.json.return_value = {'error': 'Object not found'}
                else:
                    response.json.return_value = dict(tdes_response, signature=_SCOUT_SIGNATURE)
            else:
                response.json.return_value = {
                    'signature': _SCOUT_SIGNATURE, 'count': len(roster), 'data': roster,
                }
            return response

        out = StringIO()
        with mock.patch('tom_jpl.jpl.requests.get', side_effect=fake_get):
            call_command('updatescout', stdout=out, **opts)
        return out.getvalue(), calls

    def test_unchanged_candidates_cost_a_single_request(self):
        """The whole point of the roster diff: cost tracks churn, not the size of the pool.

        Five candidates, none of which Scout has recomputed, must cost exactly one request --
        the unconstrained roster call -- and no per-object calls at all.
        """
        stored_last_run = datetime(2026, 2, 11, 22, 45, tzinfo=tzutc())
        roster = []
        for i in range(5):
            name = f'ZTF10B{i}'
            detail = ScoutDetailFactory(active=True, last_run=stored_last_run)
            detail.target.name = name
            detail.target.save()
            roster.append(make_result({'objectName': name}))

        out, calls = self._run_counting(roster, skip_designations=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual([c for c in calls if c.get('tdes')], [])
        self.assertIn('5 unchanged', out)

    def test_recomputed_candidate_is_refreshed_without_extra_requests(self):
        # One candidate has been recomputed and one has not. The recomputed one is stored from
        # its roster row and the other is skipped -- still a single request for both.
        stored_last_run = datetime(2026, 2, 11, 22, 45, tzinfo=tzutc())
        for name in ('ZTF10BL', 'ZTF10BM'):
            detail = ScoutDetailFactory(active=True, last_run=stored_last_run)
            detail.target.name = name
            detail.target.save()
        roster = [
            make_result({'objectName': 'ZTF10BL', 'lastRun': '2026-03-01 00:00', 'nObs': 8}),
            make_result({'objectName': 'ZTF10BM'}),
        ]

        out, calls = self._run_counting(roster, skip_designations=True)

        self.assertEqual(len(calls), 1)
        self.assertIn('1 unchanged', out)
        self.assertEqual(ScoutDetail.objects.get(target__name='ZTF10BL').num_obs, 8)
        self.assertEqual(ScoutDetailHistory.objects.filter(target__name='ZTF10BL').count(), 1)

    def test_empty_roster_retires_nothing(self):
        """A failed or empty fetch must never be read as "every candidate left at once"."""
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()

        out, _ = self._run_counting([], skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertTrue(scout_detail.active)
        self.assertIn('skipping reconciliation', out)

    def test_truncated_roster_retires_nothing(self):
        # Scout reports its own row count, so count != len(data) means a partial payload.
        scout_detail = ScoutDetailFactory(active=True)
        scout_detail.target.name = 'ZTF10BL'
        scout_detail.target.save()

        def fake_get(url, data=None, **kwargs):
            response = mock.Mock()
            response.json.return_value = {
                'signature': _SCOUT_SIGNATURE, 'count': 40, 'data': [make_result({'objectName': 'OTHER01'})],
            }
            return response

        out = StringIO()
        with mock.patch('tom_jpl.jpl.requests.get', side_effect=fake_get):
            call_command('updatescout', stdout=out, skip_designations=True)

        scout_detail.refresh_from_db()
        self.assertTrue(scout_detail.active)
        self.assertIn('partial list', out.getvalue())

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
    """``_update_designations`` settles Scout candidates from the MPC departure feed.

    The real fetch is mocked at the ``_fetch_mpc_departures`` boundary (the pluggable
    transport), so these exercise the settlement logic: renaming to a new designation
    (keeping the trksub as an alias), linking via ``merged_into`` when the designation is
    already held, recording lost/dne/artificial outcomes with their reference, and the
    terminal-state guarantee that a settled candidate is never reprocessed.
    """

    def setUp(self):
        self.command = Command(stdout=StringIO())
        self.scout_detail = ScoutDetailFactory(active=False)
        self.target = self.scout_detail.target
        self.target.name = 'A11Df9S'
        self.target.save()

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_renames_target_and_keeps_provisional_as_alias(self, mock_fetch):
        mock_fetch.return_value = {
            'A11Df9S': make_departure_row(designation='2026 LX', reference='MPEC 2026-L12')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        self.assertTrue(TargetName.objects.filter(name='A11Df9S', target=self.target).exists())
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.mpc_status, 'designated')
        self.assertEqual(self.scout_detail.mpc_reference, 'MPEC 2026-L12')
        self.assertIsNone(self.scout_detail.merged_into)
        self.assertIsNotNone(self.scout_detail.mpc_status_checked)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_dry_run_reports_without_writing(self, mock_fetch):
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX')}

        self.command._update_designations(dry_run=True)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.assertFalse(TargetName.objects.filter(name='A11Df9S').exists())
        self.scout_detail.refresh_from_db()
        self.assertIsNone(self.scout_detail.mpc_status)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_ignores_targets_without_scout_detail(self, mock_fetch):
        # A target whose name matches the feed but was never ingested via Scout.
        other = Target.objects.create(name='A22Eg0T', type=Target.NON_SIDEREAL)
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX'),
                                   'A22Eg0T': make_departure_row(designation='2026 MY')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        other.refresh_from_db()
        self.assertEqual(other.name, 'A22Eg0T')

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_claimed_designation_links_instead_of_renaming(self, mock_fetch):
        # Another target already carries the designation (the same body ingested under a
        # different trksub, or independently from SBDB). The name is not copied; the
        # settlement records which object this submission became.
        claimant = Target.objects.create(name='2026 LX', type=Target.NON_SIDEREAL)
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.mpc_status, 'designated')
        self.assertEqual(self.scout_detail.merged_into, '2026 LX')
        self.assertEqual(self.scout_detail.resolve_merged_into(), claimant)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_own_alias_designation_is_promoted_not_skipped(self, mock_fetch):
        # An older updatescout recorded the designation as an alias without renaming
        # ("old style"). That alias must not block the rename as a collision: the pair is
        # swapped so the designation becomes the name and the trksub the alias.
        TargetName.objects.create(target=self.target, name='2026 LX')
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        aliases = list(TargetName.objects.filter(target=self.target).values_list('name', flat=True))
        self.assertEqual(aliases, ['A11Df9S'])

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_lost_candidate_settles_with_its_reason(self, mock_fetch):
        mock_fetch.return_value = {'A11Df9S': make_departure_row(status='lost')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.mpc_status, 'lost')

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_merge_chain_outcome_records_the_surviving_identifier(self, mock_fetch):
        # This submission was identified with another trksub which then got designated;
        # the feed resolves the chain, and merged_into keeps the pairing.
        mock_fetch.return_value = {'A11Df9S': make_departure_row(
            designation='2026 LX', reference='MPEC 2026-L12', merged_into='B22Xy1Z')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.merged_into, 'B22Xy1Z')

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_settled_candidate_is_never_reprocessed(self, mock_fetch):
        self.scout_detail.mpc_status = 'lost'
        self.scout_detail.save()
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.mpc_status, 'lost')

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_active_candidate_can_be_designated(self, mock_fetch):
        # The MPC can designate an object while Scout still lists it; the rename and
        # settlement happen immediately, and reconciliation keeps matching the roster row
        # through the trksub alias until Scout drops it.
        self.scout_detail.active = True
        self.scout_detail.save()
        mock_fetch.return_value = {'A11Df9S': make_departure_row(designation='2026 LX')}

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        self.scout_detail.refresh_from_db()
        self.assertEqual(self.scout_detail.mpc_status, 'designated')
        self.assertTrue(self.scout_detail.active)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_departed_candidate_missing_from_the_page_is_stamped(self, mock_fetch):
        # The page has outcomes, just not for this object yet: stamp the attempt so the
        # pending state is visible, and leave it unsettled for later runs.
        mock_fetch.return_value = {'ZZ99999': make_departure_row(status='lost')}

        self.command._update_designations(dry_run=False)

        self.scout_detail.refresh_from_db()
        self.assertIsNone(self.scout_detail.mpc_status)
        self.assertIsNotNone(self.scout_detail.mpc_status_checked)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_fetch_failure_warns_without_raising(self, mock_fetch):
        mock_fetch.side_effect = requests.ConnectionError('blocked')

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, 'A11Df9S')
        self.scout_detail.refresh_from_db()
        self.assertIsNone(self.scout_detail.mpc_status_checked)

    @mock.patch('tom_jpl.management.commands.updatescout._fetch_mpc_departures')
    def test_retired_duplicate_we_never_held_becomes_an_alias_of_the_survivor(self, mock_fetch):
        # C33Qq7Q was identified with A11Df9S and retired; we only ever ingested A11Df9S.
        # After A11Df9S is renamed to the designation, the retired trksub is recorded as
        # one of its aliases so both identifiers stay searchable.
        mock_fetch.return_value = {
            'A11Df9S': make_departure_row(designation='2026 LX'),
            'C33Qq7Q': make_departure_row(designation='2026 LX', merged_into='A11Df9S'),
        }

        self.command._update_designations(dry_run=False)

        self.target.refresh_from_db()
        self.assertEqual(self.target.name, '2026 LX')
        self.assertTrue(TargetName.objects.filter(name='C33Qq7Q', target=self.target).exists())


class TestParseDepartures(SimpleTestCase):
    """``_parse_departures`` turns the MPC 'Previous NEOCP Objects' listing into normalized
    outcome records.

    The fixture mirrors the real page: one ``<li>`` per departure, designations as
    ``DESIG = TRKSUB (date) [see MPEC ...]`` with the reference inside anchor/italic
    markup, comets carrying a ``Comet`` prefix, identifications as ``TRKSUB = TRKSUB``,
    and phrase-style outcomes ('was not confirmed', 'does not exist', 'was not a minor
    planet'). Navigation chrome and unknown line shapes must be ignored, and
    identification chains followed to the surviving object's own outcome.
    """

    SAMPLE_HTML = """
    <html><body>
    <ul><li><a href="/mpec/RecentMPECs.html">Recent</a></li></ul>
    <ul>
    <li> 2026 QT = ST26H67 (Aug. 21.34 UT)   [see <a href="/mpec/K26/K26Q53.html"><i>MPEC</i> 2026-Q53</a>]
    <li> Comet P/2026 P1 = P12p4Jx (Aug. 25.75 UT)   [see <a href="/mpec/K26/K26Q98.html"><i>MPEC</i> 2026-Q98</a>]
    <li> (452639) = A11EXtc (July 28.51 UT)   [see <a href="/mpec/K26/K26O50.html"><i>MPEC</i> 2026-O50</a>]
    <li>ST26H89 = TF26HD1 (Aug. 24.69 UT)
    <li> 2026 QA3 = ST26H89 (Aug. 23.11 UT)   [see <a href="/mpec/K26/K26QA5.html"><i>MPEC</i> 2026-Q105</a>]
    <li> ST26H76 was not confirmed (Aug. 27.68 UT)
    <li> 19E0001 does not exist (June 20.19 UT)
    <li> 6EI3621 was not a minor planet (May 19.65 UT)
    <li>B11AAA2 = B11AAA1 (Aug. 20.00 UT)
    <li> B11AAA2 was not confirmed (Aug. 19.00 UT)
    <li>C11AAA1 = C11AAA2 (Aug. 20.00 UT)
    </ul>
    </body></html>
    """

    def test_designation_line_with_reference(self):
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['ST26H67'], make_departure_row(
            designation='2026 QT', reference='MPEC 2026-Q53'))

    def test_comet_prefix_is_stripped_from_the_designation(self):
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['P12p4Jx']['designation'], 'P/2026 P1')

    def test_numbered_designation_keeps_mpc_rendering(self):
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['A11EXtc']['designation'], '(452639)')

    def test_outcome_phrases_map_to_status_codes(self):
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['ST26H76'], make_departure_row(status='lost'))
        self.assertEqual(outcomes['19E0001'], make_departure_row(status='dne'))
        self.assertEqual(outcomes['6EI3621'], make_departure_row(status='na'))

    def test_identification_chain_inherits_the_survivors_designation(self):
        # TF26HD1 was retired into ST26H89 ('ST26H89 = TF26HD1'), and ST26H89 was then
        # designated ('2026 QA3 = ST26H89'): the chain resolves TF26HD1 to the final
        # designation, keeping the immediate survivor in merged_into.
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['TF26HD1'], make_departure_row(
            designation='2026 QA3', reference='MPEC 2026-Q105', merged_into='ST26H89'))
        # The survivor's own record is a plain designation, with no merge involved.
        self.assertEqual(outcomes['ST26H89'], make_departure_row(
            designation='2026 QA3', reference='MPEC 2026-Q105'))

    def test_identification_chain_inherits_a_failed_survivors_reason(self):
        # B11AAA1 was retired into B11AAA2 ('B11AAA2 = B11AAA1'), and B11AAA2 itself was
        # then never confirmed: the retired submission shares that fate.
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertEqual(outcomes['B11AAA1'], make_departure_row(
            status='lost', merged_into='B11AAA2'))

    def test_unresolvable_chain_yields_no_record(self):
        # C11AAA2 was retired into C11AAA1, which the page knows nothing about
        # (presumably still alive on the NEOCP): stay pending rather than guess.
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertNotIn('C11AAA2', outcomes)

    def test_navigation_chrome_is_ignored(self):
        outcomes = _parse_departures(self.SAMPLE_HTML)
        self.assertNotIn('Recent', outcomes)

    def test_empty_page_returns_empty_mapping(self):
        self.assertEqual(_parse_departures('<html><body></body></html>'), {})

    @mock.patch('tom_jpl.management.commands.updatescout.requests.get')
    def test_fetch_http_error_propagates(self, mock_get):
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.HTTPError('500')
        mock_get.return_value = response

        with self.assertRaises(requests.HTTPError):
            _fetch_mpc_departures()
