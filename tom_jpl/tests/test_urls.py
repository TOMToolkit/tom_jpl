# tom_jpl/test_urls.py for testing the URL configuration of the tom_jpl app.
from django.urls import path, include

urlpatterns = [
    path('targets/', include('tom_targets.urls', namespace='targets')),
]
