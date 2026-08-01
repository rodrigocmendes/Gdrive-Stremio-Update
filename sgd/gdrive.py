import requests
from datetime import datetime, timedelta
from sgd.cache import Pickle, Json
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

class GoogleDrive:
    def __init__(self, token, folder_id=None):
        self.token = token
        self.page_size = 1000
        self.folder_id = folder_id or None
        self.acc_token = Pickle("acctoken.pickle")
        self.drive_names = Json("drivenames.json")
        self.folder_ids_cache = Json("folderids.json")

        creds = Credentials.from_authorized_user_info(self.token)
        self.drive_instance = build("drive", "v3", credentials=creds)

    @staticmethod
    def qgen(string, chain="and", splitter=" ", method=None):
        out = ""

        get_method = lambda _: method
        if not method:
            get_method = lambda w: "fullText" if w.isdigit() else "name"

        for word in string.split(splitter):
            if out:
                out += f" {chain} "
            out += f"{get_method(word)} contains '{word}'"
        return out

    def get_query(self, sm):
        out = []

        if sm.stream_type == "series":
            seep_q = self.qgen(
                f"s{sm.se} e{sm.ep}, "
                f"s{int(sm.se)} e{int(sm.ep)}, "
                f"season {int(sm.se)} episode {int(sm.ep)}, "
                f'"{int(sm.se)} x {int(sm.ep)}", '
                f'"{int(sm.se)} x {sm.ep}"',
                chain="or",
                splitter=", ",
                method="fullText",
            )
            for title in sm.titles:
                if len(title.split()) == 1:
                    out.append(
                        f"fullText contains '\"{title}\"' and "
                        f"name contains '{title}' and ({seep_q})"
                    )
                else:
                    out.append(f"{self.qgen(title)} and ({seep_q})")
        else:
            for title in sm.titles:
                if len(title.split()) == 1:
                    out.append(
                        f"fullText contains '\"{title}\"' and "
                        f"name contains '{title}' and "
                        f"fullText contains '\"{sm.year}\"'"
                    )
                else:
                    out.append(self.qgen(f"{title} {sm.year}"))
        return out

    def get_folder_ids(self):
        """Recursively collects the root folder id and every subfolder id
        beneath it, so file searches can be scoped to that folder tree.
        Result is cached for 1 hour to avoid re-walking the tree on every
        stream request."""
        if not self.folder_id:
            return None

        cache_key = f"ids_{self.folder_id}"
        cached = self.folder_ids_cache.contents.get(cache_key)
        if cached and datetime.fromisoformat(cached["expires"]) > datetime.now():
            return cached["ids"]

        all_ids = set()
        to_visit = [self.folder_id]

        while to_visit:
            current = to_visit.pop()
            if current in all_ids:
                continue
            all_ids.add(current)

            page_token = None
            while True:
                response = self.drive_instance.files().list(
                    q=(
                        f"'{current}' in parents and "
                        "mimeType='application/vnd.google-apps.folder' and "
                        "trashed=false"
                    ),
                    fields="nextPageToken, files(id)",
                    pageSize=self.page_size,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    corpora="allDrives",
                ).execute()

                to_visit.extend(f["id"] for f in response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        self.folder_ids_cache.contents[cache_key] = {
            "ids": list(all_ids),
            "expires": (datetime.now() + timedelta(hours=1)).isoformat(),
        }
        self.folder_ids_cache.save()
        return list(all_ids)

    def file_list(self, file_fields):
        def callb(request_id, response, exception):
            if response:
                output.extend(response.get("files"))

        if not self.query:
            return None

        output = []
        files = self.drive_instance.files()
        batch = self.drive_instance.new_batch_http_request()

        folder_ids = self.get_folder_ids()
        parents_q = ""
        if folder_ids:
            parents_q = " and (" + " or ".join(
                f"'{fid}' in parents" for fid in folder_ids
            ) + ")"

        for q in self.query:
            batch_inst = files.list(
                q=f"{q} and trashed=false and mimeType contains 'video/'{parents_q}",
                fields=f"files({file_fields})",
                pageSize=self.page_size,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            )
            batch.add(batch_inst, callback=callb)
        batch.execute()
        return output

    def get_drive_names(self):
        def callb(request_id, response, exception):
            if response:
                drive_id = response.get("id")
                drive_name = response.get("name")
                self.drive_names.contents[drive_id] = drive_name

        batch = self.drive_instance.new_batch_http_request()
        drives = self.drive_instance.drives()
        drive_ids = set(item["driveId"] for item in self.results if item.get("driveId"))

        for drive_id in drive_ids:
            cached_drive_name = self.drive_names.contents.get(drive_id)
            if not cached_drive_name:
                self.drive_names.contents[drive_id] = None
                batch_inst = drives.get(driveId=drive_id, fields="name, id")
                batch.add(batch_inst, callback=callb)

        batch.execute()
        self.drive_names.save()
        return self.drive_names.contents

    def search(self, stream_meta):
        self.results = []
        self.query = self.get_query(stream_meta)

        response = self.file_list("id, name, size, driveId, md5Checksum")
        self.len_response = 0

        if response:
            self.len_response = len(response)
            uids = set()

            def check_dupe(item):
                driveId = item.get("driveId", "MyDrive")
                md5Checksum = item.get("md5Checksum")
                uid = driveId + md5Checksum

                if uid in uids:
                    return False

                uids.add(uid)
                return True

            self.results = sorted(
                filter(check_dupe, response),
                key=lambda item: int(item.get("size")),
                reverse=True,
            )

        self.get_drive_names()
        return self.results

    def get_acc_token(self):
        token_exipred = (
            self.acc_token.contents.get("expires_in", datetime.now()) <= datetime.now()
        )

        if token_exipred or not self.acc_token.contents:
            body = {
                "client_id": self.token["client_id"],
                "client_secret": self.token["client_secret"],
                "refresh_token": self.token["refresh_token"],
                "grant_type": "refresh_token",
            }
            api_url = "https://www.googleapis.com/oauth2/v4/token"
            oauth_resp = requests.post(api_url, json=body).json()
            oauth_resp["expires_in"] = (
                timedelta(seconds=oauth_resp["expires_in"]) + datetime.now()
            )

            self.acc_token.contents = oauth_resp
            self.acc_token.save()

        expiry = self.acc_token.contents["expires_in"]
        acc_token = self.acc_token.contents.get("access_token")
        print("Fetched access_token, will expire at", expiry)
        return acc_token
