from django.test import SimpleTestCase, tag, TestCase

from tom_jpl.jpl import ScoutDataService
from tom_jpl.management.commands.updatescout import _fetch_mpc_departures


@tag('canary')
class TestScoutDataServiceCanary(TestCase):
    """Tests that actually hit the JPL Scout API."""

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
        self.expected_result_keys = ['lastRun', 'neo1kmScore', 'phaScore', 'geocentricScore', 'arc', 'rate',
                                     'neoScore', 'rating', 'elong', 'uncP1', 'vInf', 'objectName', 'dec', 'H',
                                     'caDist', 'moid', 'ra', 'unc', 'Vmag', 'nObs', 'rmsN', 'tEphem',
                                     'tisserandScore', 'ieoScore']

    # Basic test to ensure that the canary test suite is running correctly
    def test_boilerplate(self):
        self.assertTrue(True)

    def _pick_live_target(self):
        """Pick a live target from the JPL Scout API since we need a real object's temporary designation (tdes)
        and objects only live for about a week."""

        # Set a fully permissive set of parameters to get max no. of results and pick the first one.
        # We don't care about the actual results, just that we get a live target.
        permissive_params = dict(self.input_parameters, geo_score_max=None)
        results = self.jpl_ds.query_service(self.jpl_ds.build_query_parameters(permissive_params))
        if not results or len(results) == 0:
            self.skipTest('No live targets available from JPL Scout API at this time.')
        return results[0]['objectName']  # type: ignore

    def test_query_service(self):
        """Test query_service."""
        results = self.jpl_ds.query_service(self.jpl_ds.build_query_parameters(self.input_parameters))

        self.assertIsNotNone(results)
        self.assertIsInstance(results, list)
        for key in results[0].keys():  # type: ignore
            self.assertIn(key, self.expected_result_keys)

    def test_query_targets_single(self):
        """Test query_targets with a single result."""
        tdes = self._pick_live_target()
        permissive_params = dict(self.input_parameters, tdes=tdes, geo_score_max=None)
        results = self.jpl_ds.query_targets(permissive_params)

        self.assertIsNotNone(results)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['objectName'], tdes)
        self.assertIn('scout_detail', results[0])

    def test_create_target_from_query(self):
        """Test create_target_from_query."""
        tdes = self._pick_live_target()
        detail = self.jpl_ds.query_service(self.jpl_ds.build_query_parameters(dict(tdes=tdes)))
        target = self.jpl_ds.create_target_from_query(detail[0])

        target_detail = detail[0]
        self.assertIsNotNone(target)
        self.assertEqual(target.name, tdes)
        self.assertIsNotNone(target.eccentricity)
        idx = target_detail['orbits']['fields'].index('H')
        self.assertAlmostEqual(float(target_detail['orbits']['data'][0][idx]), target.abs_mag, places=2)
        idx = target_detail['orbits']['fields'].index('epoch')
        elem_epoch = float(target_detail['orbits']['data'][0][idx][2:]) - 0.5
        self.assertAlmostEqual(elem_epoch, target.epoch_of_elements, places=2)


@tag('canary')
class TestFetchMpcDeparturesCanary(SimpleTestCase):
    """Hits the live MPC 'Previous NEOCP Objects' page (ToConfirm_PrevDes.html) and checks
    the parser still understands its line shapes. The page's window spans months, so a
    healthy fetch always yields both designated and undesignated outcomes."""

    def test_fetches_and_parses_live_page(self):
        outcomes = _fetch_mpc_departures()
        self.assertIsInstance(outcomes, dict)
        # Months of departures: if this is small, the page layout probably changed.
        self.assertGreater(len(outcomes), 100)
        statuses = {row['status'] for row in outcomes.values()}
        self.assertIn('designated', statuses)
        self.assertIn('lost', statuses)
        for trksub, row in outcomes.items():
            self.assertTrue(trksub)
            if row['status'] == 'designated':
                self.assertTrue(row['designation'])
            else:
                self.assertIsNone(row['designation'])
