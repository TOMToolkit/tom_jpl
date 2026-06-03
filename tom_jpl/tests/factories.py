import factory

from tom_targets.models import Target
from tom_jpl.models import ScoutDetail


class SiderealTargetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Target

    name = factory.Faker('pystr')
    type = Target.SIDEREAL
    ra = factory.Faker('pyfloat', min_value=-90, max_value=90)
    dec = factory.Faker('pyfloat', min_value=-90, max_value=90)
    epoch = factory.Faker('pyfloat')
    pm_ra = factory.Faker('pyfloat')
    pm_dec = factory.Faker('pyfloat')


class NonSiderealTargetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Target

    name = factory.Faker('pystr')
    type = Target.NON_SIDEREAL
    scheme = factory.Faker('random_element', elements=[s[0] for s in Target.TARGET_SCHEMES])
    mean_anomaly = factory.Faker('pyfloat')
    arg_of_perihelion = factory.Faker('pyfloat')
    lng_asc_node = factory.Faker('pyfloat')
    inclination = factory.Faker('pyfloat')
    mean_daily_motion = factory.Faker('pyfloat')
    semimajor_axis = factory.Faker('pyfloat')
    ephemeris_period = factory.Faker('pyfloat')
    ephemeris_period_err = factory.Faker('pyfloat')
    ephemeris_epoch = factory.Faker('pyfloat')
    ephemeris_epoch_err = factory.Faker('pyfloat')


class ScoutDetailFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ScoutDetail

    target = factory.SubFactory(NonSiderealTargetFactory)  # assuming this exists in factories.py
    num_obs = factory.Faker('random_int', min=1, max=100)
    neo_score = factory.Faker('random_int', min=0, max=100)
    neo1km_score = factory.Faker('random_int', min=0, max=100)
    pha_score = factory.Faker('random_int', min=0, max=100)
    ieo_score = factory.Faker('random_int', min=0, max=100)
    geocentric_score = factory.Faker('random_int', min=0, max=100)
    impact_rating = factory.Faker('random_int', min=0, max=4)
    ca_dist = factory.Faker('pyfloat', min_value=0, max_value=100, right_digits=2)
    arc = factory.Faker('pyfloat', min_value=0, max_value=30, right_digits=2)
    rms = factory.Faker('pyfloat', min_value=0, max_value=5, right_digits=2)
    uncertainty = factory.Faker('pyfloat', min_value=0, max_value=100, right_digits=2)
    uncertainty_p1 = factory.Faker('pyfloat', min_value=0, max_value=100, right_digits=2)
    vmag = factory.Faker('pyfloat', min_value=10, max_value=25, right_digits=2)
    rate = factory.Faker('pyfloat', min_value=0, max_value=100, right_digits=2)
    ra = factory.Faker('pyfloat', min_value=0, max_value=360, right_digits=4)
    dec = factory.Faker('pyfloat', min_value=-90, max_value=90, right_digits=4)
    t_ephem = factory.Faker('date_time_this_year')
    last_run = factory.Faker('date_time_this_year')
