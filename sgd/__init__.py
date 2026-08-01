import os
import json
from flask import Flask
from sgd.gdrive import GoogleDrive

app = Flask(__name__)
tokenFromVar=os.environ.get('TOKEN')
token = json.loads(tokenFromVar)
folder_id = os.environ.get('GDRIVE_FOLDER_ID') or None
gdrive = GoogleDrive(token, folder_id=folder_id)

from sgd import routes
