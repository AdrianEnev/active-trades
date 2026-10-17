# Hosting exit chart images

**Charts Exit** contains a clickable reference URL to the generated chart.
Opening the link shows the saved PNG with the trade's exact entry, stop,
target, and timing overlay. An ordinary TradingView symbol URL does not preserve
those trade-specific annotations. The PNG is linked rather than embedded in the
spreadsheet, which can remain private.

## Default: hosted uploads with no setup

By default, the journal uploads chart PNGs to [qu.ax](https://qu.ax/) and
verifies the public image before writing its reference. You do not need to run a
server, create an account, supply an API key, or configure a storage bucket.
Install the journal dependencies from the `active_trades` project directory:

```sh
pip install -e ".[journal]"
```

The existing Google service-account credentials are still used for the private
spreadsheet. They are never sent to the image host. Only the generated chart PNG
is uploaded using the service's public API, as demonstrated by its
[official uploader configuration](https://qu.ax/static/quax.sxcu).

The publisher requests the host's **Permanent** option (`expiry=-1`) and accepts
the response only when it confirms no expiration (`expires=null`). This avoids
deliberately temporary links; availability still depends on the hosting service.
No external hosting service can guarantee that a link will remain available
forever.

## Public chart contents

Anyone who obtains an image URL can view its chart, including the instrument,
trade direction, entry/stop/target levels, and trade timing. Sharing the image
does not grant access to the private spreadsheet or service account. Images are
uploaded to the selected hosting provider so that the resulting references work
when opened.

## Retry and integrity behavior

Each upload response must confirm success, no expiration, and one HTTPS viewer
URL on `qu.ax`. The journal saves that reference and the PNG's SHA-256 digest
in an adjacent `.quax.json` sidecar before checking public access. The sidecar
is written atomically. Keep these sidecars with the locally generated PNGs.

If public verification fails, the next attempt reuses the saved reference,
including after a restart. The host does not provide create-only uploads: losing
the upload response, failing to save the reference, or deleting the local cache
can result in another upload and an unreferenced hosted copy.

The viewer URL is the clickable reference written to the spreadsheet. The
journal verifies the corresponding direct PNG at `https://qu.ax/x/<slug>.png`.
It sends the public viewer URL as the `Referer` header, matching the image request
a browser makes when opening that viewer. No login or cookies are required.
That response must be direct, successful, served as `image/png`, and match the
exact local PNG's SHA-256 digest. The chart reference is written only after these
checks pass. HTTP and authentication errors are reported without secrets or
remote response bodies. Links are stable host URLs, without expiring tokens.

## Optional: an existing Google Cloud Storage bucket

If you prefer to use a dedicated Google Cloud Storage bucket, setting
`ACTIVE_TRADES_JOURNAL_CHART_BUCKET` selects it instead of qu.ax. This is optional;
the default requires no bucket. The application never creates a bucket, enables
public access, changes bucket IAM, or writes object ACLs.

1. Use a dedicated bucket for these images in your Google Cloud project. Use a
   bucket name only, without `gs://` or a URL. Google Cloud billing and storage
   charges apply according to your project settings.
2. Grant the service account from the existing local journal credentials file
   **Storage Object Creator** (`roles/storage.objectCreator`) on this bucket.
   It does not need bucket administration, object deletion, or object replacement
   permission.
3. Enable anonymous object reads by granting **Storage Object Viewer**
   (`roles/storage.objectViewer`) to `allUsers` on this bucket. Public access
   prevention or an organization policy can prohibit this. Follow
   [Google's public-read setup](https://cloud.google.com/storage/docs/access-control/making-data-public).
   The bucket-level public viewer role also allows listing objects in that bucket,
   so keep unrelated or private files elsewhere.
4. Set the bucket name in the journal environment configuration:

   ```dotenv
   ACTIVE_TRADES_JOURNAL_CHART_BUCKET=your-dedicated-exit-chart-bucket
   ```

The GCS publisher uses the local JSON file selected by
`ACTIVE_TRADES_JOURNAL_CREDENTIALS` with the Cloud Storage read/write OAuth scope.
Keep that file local. It uses deterministic object names and the
`ifGenerationMatch=0` precondition to avoid replacing an existing object. On
retry, the stored public PNG must match the local PNG before its stable
`https://storage.googleapis.com/...` URL is used. A private bucket leaves chart
references pending until its public-read policy is corrected. See the
[Cloud Storage object insert API](https://cloud.google.com/storage/docs/json_api/v1/objects/insert).
