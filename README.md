# JPL TOM Dataservice Module

This module adds JPL [SCOUT](https://cneos.jpl.nasa.gov/scout/intro.html) support to the TOM
Toolkit. Using this module TOMs can query SCOUT NEO Candidate data.

## Installation

Install the module into your TOM environment:

    pip install tom-jpl

Include the app in your `INSTALLED_APPS` in your TOM's `settings.py`:

    INSTALLED_APPS = [
        ...
        'tom_jpl',
    ]