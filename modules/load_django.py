import os
import sys
import django


sys.path.append("/home/oleks/Works/Selogercom/selogercom_project")

os.environ["DJANGO_SETTINGS_MODULE"] = "selogercom_project.settings"
django.setup()
