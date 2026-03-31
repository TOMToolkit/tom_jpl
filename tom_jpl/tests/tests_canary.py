from django.test import tag, TestCase

from tom_jpl.jpl import ScoutDataService


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

    def test_boilerplate(self):
        self.assertTrue(True)

    def test_query_service(self):
        """Test query_service."""
        results = self.jpl_ds.query_service(self.jpl_ds.build_query_parameters(self.input_parameters))

        self.assertIsNotNone(results)
        self.assertIsInstance(results, list)
        for key in results[0].keys():  # type: ignore
            self.assertIn(key, self.expected_result_keys)

    def test_query_targets_single(self):
        """Test query_targets with a single result."""
        pass

    def test_create_target_from_query(self):
        """Test create_target_from_query."""
        pass
