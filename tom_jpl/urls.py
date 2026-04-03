from django.urls import path

from .views import ScoutDetailListView

app_name = 'tom_jpl'

urlpatterns = [
    path('', ScoutDetailListView.as_view(), name='list'),
]
