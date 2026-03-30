from django.apps import AppConfig


class TomJPLConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tom_jpl'

    def data_services(self):
        """
        integration point for including data services in the TOM
        This method should return a list of dictionaries containing dot separated DataService classes
        """
        # TODO: explain in the doc string how this dict and its items are used
        return [
            {'class': f'{self.name}.jpl.ScoutDataService'},
        ]
