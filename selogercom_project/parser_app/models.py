from django.db import models

class Product(models.Model):
    location = models.TextField(null=True, blank=True)
    price = models.CharField(max_length=20, null=True, blank=True)
    price_m2 = models.CharField(max_length=20, null=True, blank=True)

    link = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=10, default="New", null=True, blank=True)

    title = models.TextField(null=True, blank=True)
    surface = models.CharField(max_length=20, null=True, blank=True)
    type = models.CharField(max_length=50, null=True, blank=True)
    rooms = models.CharField(max_length=20, null=True, blank=True)
    description = models.TextField(null=True, blank=True)

class Location(models.Model):
    name = models.CharField(max_length=200)
    url = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=10, default="New", null=True, blank=True)
