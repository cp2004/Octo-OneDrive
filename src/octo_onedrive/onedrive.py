import base64
import logging
import os
import threading
import urllib.parse
from typing import Optional

import requests
from cryptography.fernet import Fernet, InvalidToken
from dateutil.parser import parse
from msal import PublicClientApplication, SerializableTokenCache

GRAPH_URL = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT = 5  # Seconds
UNKNOWN_ERROR = "Unknown error, check octoprint.log for details"


# Built on the assumption we will only ever have one account logged in at a time
class OneDriveComm:
    def __init__(
        self,
        app_id,
        scopes,
        token_cache_path,
        authority="https://login.microsoftonline.com/common",
        encryption_key=None,
        logger="octo_onedrive.OneDriveComm",
    ):
        """
        OneDrive Communication class

        Parameters
        ----------
        app_id: str
            The application ID for the app registered with Microsoft
        scopes: list
            The scopes to request access to
        token_cache_path: str
            The path to the token cache file
        encryption_key: str
            The encryption key to use for the token cache
        logger: str
            The logger to use for logging
        """
        self._logger = logging.getLogger(logger)
        self.cache = PersistentTokenStore(token_cache_path, encryption_key)
        self.cache.load()

        self.client = PublicClientApplication(
            app_id,
            authority=authority,
            token_cache=self.cache,
        )
        self.scopes = scopes

        self.auth_poll_thread: Optional[threading.Thread] = None
        self.flow_in_progress: Optional[dict] = None

    def begin_auth_flow(self, on_success: callable, on_error: callable) -> dict:
        if self.auth_poll_thread is None or not self.auth_poll_thread.is_alive():
            # Remove any accounts before adding a new one
            self.forget_account()

            # Begin auth flow
            self.flow_in_progress = self.client.initiate_device_flow(scopes=self.scopes)
            # Thread to poll graph for auth result
            self.auth_poll_thread = threading.Thread(
                target=self.acquire_token,
                kwargs={
                    "flow": self.flow_in_progress,
                    "on_success": on_success,
                    "on_error": on_error,
                },
            )
            self.auth_poll_thread.start()
            return self.flow_in_progress
        else:
            raise AuthInProgressError("Auth flow is already in progress")

    def acquire_token(
        self, flow: dict, on_success: callable, on_error: callable
    ) -> None:
        response = self.client.acquire_token_by_device_flow(flow)
        if "access_token" in response:
            # Flow was successful
            if callable(on_success):
                on_success(response)
            self.flow_in_progress = None
        else:
            if callable(on_error):
                on_error(response)

        self.cache.save()

    def list_accounts(self):
        return [account["username"] for account in self.client.get_accounts()]

    def forget_account(self):
        if len(self.client.get_accounts()):
            # Assuming that we never get more than one account
            self.client.remove_account(self.client.get_accounts()[0])

    def list_files_and_folders(self, folder_id=None):
        response = self._list(folder_id)
        if "error" in response:
            return response  # Don't post-process, just send the message on

        items = []
        for item in response:
            if "folder" in item:
                item_type = "folder"
            elif "file" in item:
                item_type = "file"
            else:
                # Unknown type so ignore
                continue

            new_item = {
                "type": item_type,
                "name": item["name"],
                "id": item["id"],
                "parent": item["parentReference"]["id"],
                "path": item["parentReference"]["path"].split("/root:")[1]
                + "/"
                + item["name"],  # Human readable path
            }

            if item_type == "folder":
                new_item["childCount"] = item["folder"]["childCount"]

            elif item_type == "file":
                # We'll need this information to determine whether to sync the file
                new_item["eTag"] = item["eTag"]
                # If this can be stored along with the file in OctoPrint, it will make life
                # WAY easier to compare whether files have changed.
                # For two way sync I will need to know which is newer though
                new_item["lastModified"] = parse(
                    item["lastModifiedDateTime"]
                ).timestamp()
                # Convert to unix timestamp, like OctoPrint's internal store for comparison
                new_item["downloadUrl"] = (item["@microsoft.graph.downloadUrl"],)

            items.append(new_item)

        return {"root": True if folder_id is None else False, "items": items}

    def list_folders(self, folder_id=None):
        response = self._list(folder_id)
        if "error" in response:
            return response  # Don't post-process, just send the message on

        folders = []
        for item in response:
            if "folder" in item:
                folders.append(
                    {
                        "name": item["name"],
                        "id": item["id"],
                        "parent": item["parentReference"]["id"],
                        "childCount": item["folder"]["childCount"],
                        "path": item["parentReference"]["path"].split("/root:")[1]
                        + "/"
                        + item["name"],  # Human readable path
                    }
                )

        return {"root": True if folder_id is None else False, "folders": folders}

    def _list(self, item_id=None):
        if not len(self.client.get_accounts()):
            self._logger.error("No accounts registered, can't list folders")
            return {"error": {"message": "No accounts registered"}}

        if item_id is None:
            location = "root"
        else:
            location = f"items/{item_id}"

        data = self._graph_request(f"/me/drive/{location}/children")

        if "error" in data:
            return {"error": data["error"]}  # No extra fields slipping in
        else:
            return data["value"]

    def file_info(self, name=None, id=None, root=None):
        if not name and not id:
            raise ValueError("Either name or id must be provided")

        if name[0] == "/":
            name = name[1:]

        # Format a URL, either relative from root or given folder, or absolute with id
        item_url = (
            f"/me/drive/items/{root if root else 'root'}:/{name}"
            if name
            else f"/me/drive/items/{id}"
        )

        data = self._graph_request(item_url)

        if "error" in data:
            return {"error": data["error"]}  # No extra fields slipping in
        else:
            return data

    def upload_file(
        self,
        file_name,
        file_path,
        upload_location_id,
        on_upload_progress=lambda x: None,
        on_upload_complete=lambda: None,
        on_upload_error=lambda x: None,
    ):
        # https://docs.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0

        if not len(self.client.get_accounts()):
            self._logger.error("No accounts registered, can't upload file")
            return

        if not callable(on_upload_progress):
            raise TypeError("on_progress_update must be callable")

        if not callable(on_upload_complete):
            raise TypeError("on_upload_complete must be callable")

        if not callable(on_upload_error):
            raise TypeError("on_error must be callable")

        self._logger.info(f"Starting upload session for {file_name}")

        # Get file details
        if not os.path.exists(file_path):
            self._logger.error(f"File {file_path} does not exist")
            # Abort
            return

        file_size = os.path.getsize(file_path)

        self._logger.debug("Creating upload session")
        data = {
            "item": {
                "@microsoft.graph.conflictBehavior": "rename",
                "name": file_name,
                "fileSize": file_size,
            }
        }
        upload_session = self._graph_request(
            f"/me/drive/items/{upload_location_id}:/{urllib.parse.quote(file_name)}:/createUploadSession",
            method="POST",
            data=data,
        )

        if (
            upload_session
            and "error" in upload_session
            or "uploadUrl" not in upload_session
        ):
            self._logger.error(
                f"Error creating upload session: {upload_session['error']}"
            )
            return

        # Upload URLs will expire in several days, but that shouldn't be a problem for us
        upload_url = upload_session["uploadUrl"]

        # Maximum bytes in any one request is 60MiB. So we need to chunk the file, which must be
        # a multiple of 320KiB. See docs. Recommended 5-10MB chunks.
        chunk_size = 1024 * 320 * 16  # 5MB
        number_of_uploads = -(-file_size // chunk_size)
        self._logger.debug(
            f"chunk size: {chunk_size}, file size: {file_size}, number of uploads: {number_of_uploads}"
        )

        self._logger.info("Uploading file to OneDrive...")

        try:
            self._logger.debug("Loading file")

            i = 0
            with open(file_path, "rb") as f:
                while f.tell() < file_size:
                    i += 1

                    content_range_start = f.tell()
                    content_range_end = (
                        f.tell() + chunk_size - 1
                    )  # -1 because f.tell() is 0-indexed

                    # Last chunk is capped of course
                    if (file_size - f.tell()) < chunk_size:
                        content_range_end = file_size - 1

                    self._logger.debug(f"Uploading chunk {i} of {number_of_uploads}")
                    self._logger.debug(
                        f"content_range_start: {content_range_start}, content_range_end: {content_range_end}"
                    )
                    # Notify of upload progress, as integer percentage
                    on_upload_progress((100 * i) // number_of_uploads)

                    chunk = f.read(chunk_size)

                    # This was the site of 3 days of pain
                    headers = self._get_headers()
                    headers.update(
                        {
                            "Content-range": f"bytes {content_range_start}-{content_range_end}/{file_size}"
                        }
                    )

                    response = self._graph_request(
                        upload_url,
                        method="PUT",
                        data=chunk,
                        headers=headers,
                        timeout=60,  # Longer timeout than default as we are uploading larger things
                    )

                    if "error" in response:
                        self._logger.error(
                            f"Error uploading chunk {i}: {response['error']}"
                        )
                        on_upload_error(response["error"])
                        return

                    # TODO check status of upload
                    # 202 = still uploading
                    # 201/200 = complete

                    self._logger.debug(f"Chunk {i} upload complete")

        except Exception as e:
            self._logger.error(f"Error uploading file: {e}")
            on_upload_error(repr(e))
            return

        # If we got this far... Everything worked?
        self._logger.info("Upload complete")
        on_upload_complete()

        return {
            "id": response.get("id", ""),
            "eTag": response.get("eTag", ""),
        }

    def download_file(self, folder_id, file_name):
        # https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems
        # DriveItems can be addressed by path as above
        if file_name[0] != "/":
            file_name = f"/{file_name}"

        item_url = f"{GRAPH_URL}/me/drive/items/{folder_id}:{urllib.parse.quote(file_name)}:/content"

        try:
            # try/except catch all for internet issues
            with requests.get(item_url, headers=self._get_headers(), stream=True) as r:
                error = self._check_status(r)
                if type(error) == dict and "error" in error:
                    return {"error": error["error"]}

                from tempfile import NamedTemporaryFile

                with NamedTemporaryFile(delete=False) as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

        except Exception as e:
            self._logger.exception(e)
            return {"error": UNKNOWN_ERROR}

        return {"name": file_name, "path": f.name}

    def delete_file(self, folder_id, file_name):
        # https://docs.microsoft.com/en-us/graph/api/driveitem-delete?view=graph-rest-1.0&tabs=http
        if file_name[0] != "/":
            file_name = f"/{file_name}"

        item_url = (
            f"{GRAPH_URL}/me/drive/items/{folder_id}:{urllib.parse.quote(file_name)}"
        )

        try:
            # try/except catch all for internet issues
            response = self._graph_request(item_url, method="DELETE")
            if "error" in response:
                return {"error": response["error"]}

        except Exception as e:
            self._logger.exception(e)
            return {"error": UNKNOWN_ERROR}

    def _get_headers(self) -> dict:
        token = self.client.acquire_token_silent_with_error(
            scopes=self.scopes, account=self.client.get_accounts()[0]
        )

        if "error" in token:
            self._logger.error("Error getting token: " + token["error"])
            return {}  # Will end up with empty token error later

        if token is None:
            self._logger.error(
                "No token available in cache to use"
            )  # Probably no account logged in
            return {}  # Will end up with empty token error later

        return {"Authorization": f"Bearer {token['access_token']}"}

    def _graph_request(
        self,
        endpoint,
        method="GET",
        select=None,
        data=None,
        timeout=REQUEST_TIMEOUT,
        headers=None,
    ) -> dict:
        if endpoint.startswith("https"):
            url = endpoint
        else:
            if not endpoint[:1] == "/":
                endpoint = f"/{endpoint}"
            url = f"{GRAPH_URL}{endpoint}"

        select = {"$select": select} if select is not None else None

        headers = headers if headers is not None else self._get_headers()

        # Catch-all in case of internet problems
        try:
            response = requests.request(
                method,
                url,
                params=select,
                data=data,
                headers=headers,
                timeout=timeout,
            )

        except Exception as e:
            self._logger.exception(e)
            return {"error": UNKNOWN_ERROR}

        error = self._check_status(response)
        if error:
            return error

        # Finally, try return a json response
        try:
            return response.json()
        except Exception as e:
            self._logger.exception(e)
            return {"error": UNKNOWN_ERROR}

    def _check_status(self, response):
        try:
            # Check status code - all errors will have an error code outside 2xx-3xx
            response.raise_for_status()

        except requests.RequestException as e:
            self._logger.exception(e)
            # Try and get the error message out of MS graph response - all 'successful' network requests
            # should (by protocol) have a useful error message, but if not, return a generic error
            try:
                data = response.json()
                if "error" in data:
                    return {"error": data["error"]}
            except Exception as e:
                self._logger.exception(e)
                return {"error": UNKNOWN_ERROR}


class AuthInProgressError(Exception):
    pass


class PersistentTokenStore(SerializableTokenCache):
    """
    Subclasses the default TokenCache, to write it out a file path
    Pass to the client instance as below
    Usage:
        self.cache = PersistentTokenStore(os.path.join(self.plugin.get_plugin_data_folder(), "cache.bin"))
        self.cache.load()

        self.client = PublicClientApplication(
            token_cache=self.cache,
            )
    """

    def __init__(
        self, path, secret_key=None, logger="octo_onedrive.PersistentTokenStore"
    ):
        super().__init__()
        self.path = path
        self._logger = logging.getLogger(logger)

        if not isinstance(secret_key, str) and secret_key is not None:
            raise TypeError("secret_key must be a string, or None")

        if secret_key is None:
            self._logger.warning("No secret key provided, cache will be unencrypted")

        self.secret_key = secret_key

    def save(self) -> None:
        """Serialize the current cache state into a string."""
        if self.has_state_changed:
            try:
                with open(self.path, "wb") as file:
                    file.write(self._encrypt(self.serialize()))
            except Exception as e:
                self._logger.error("Failed to write token cache")
                self._logger.exception(e)

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, mode="rb") as file:
                    content = file.read()
                    self.deserialize(self._decrypt(content))
            except Exception as e:
                self._logger.error("Failed to read token cache")
                self._logger.exception(e)
                # Just load empty cache
                self.deserialize("{}")

    def add(self, event, **kwargs):
        super().add(event, **kwargs)
        self.save()

    def modify(self, credential_type, old_entry, new_key_value_pairs=None):
        super().modify(credential_type, old_entry, new_key_value_pairs)
        self.save()

    def _get_encryption_key(self) -> bytes:
        if self.secret_key is not None:
            return base64.urlsafe_b64encode(self.secret_key.encode("utf-8"))

    def _encrypt(self, data: str) -> bytes:
        data = data.encode("utf-8")

        if not self.secret_key:
            return data

        f = Fernet(self._get_encryption_key())
        return f.encrypt(data)

    def _decrypt(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")

        if not self.secret_key:
            return data.decode("utf-8")

        try:
            f = Fernet(self._get_encryption_key())
            return f.decrypt(data).decode("utf-8")
        except InvalidToken:
            self._logger.error("Failed to decrypt token cache")
            return "{}"
