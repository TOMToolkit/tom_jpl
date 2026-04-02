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

    def target_detail_tabs(self):
        """
        Integration point for adding tabs to the target detail page.

        This method should return a list of dictionaries that include a `partial` key pointing to the path of the html
        target_detail_tab partial.
        The `context` key should point to the dot separated string path to the templatetag that will return a
        dictionary containing new context for the accompanying partial.
        The `label` key will represent the label string to put in the tab and use as a tab reference id.

        This partial will be displayed within the tab on the target detail page.

        """
        return [{'partial': f'{self.name}/partials/scoutdetails_partial.html',
                 'label': 'Scout Details',
                 'context': f'{self.name}.templatetags.scoutdetail_extras.tab_context'
                 }]
