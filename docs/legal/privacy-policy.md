# OpenFit — Privacy Policy

_Last updated: 14 October 2026_

## What OpenFit is

OpenFit is free, open-source, **self-hosted** software. You run your own
instance on hardware you control; there is no OpenFit cloud service and no
central server operated by the developer. Each instance is operated by, and
under the control of, the person who runs it. This policy describes how an
OpenFit instance handles data.

## What data OpenFit accesses

With your explicit authorization, OpenFit reads your own health and fitness
data from the services you choose to connect, in order to collate it in one
place you own. Depending on which sources you connect, this may include:

- **Google Health API** (e.g. Google Pixel Watch / Fitbit data): metrics such
  as steps, sleep, heart rate, and similar activity and health measurements
  that you authorize.
- **Other device sources** you connect (for example Garmin): comparable
  activity, sleep, and health metrics.

OpenFit only accesses the categories of data covered by the permissions you
grant on the provider's consent screen, and only for the account you connect.

## How OpenFit uses your data

Your data is used solely to display and collate your own health information
within your own OpenFit instance — showing what data has been collected, how
continuous it is, and which source each value came from, and making it
available to applications you run against your own instance. It is not used for
any other purpose.

## How your data is stored

All data OpenFit retrieves is stored locally, in your instance's own database,
on hardware you control. Credentials and access tokens for connected services
(including Google OAuth tokens) are **encrypted at rest**. Nothing is stored on
any server operated by the developer.

## What OpenFit does *not* do

- It does **not** transmit your health data to the developer or to any third
  party. The only outbound network calls an instance makes are to the APIs of
  the services you have connected (for example Google or Garmin), in order to
  fetch **your own** data.
- It contains **no** telemetry, analytics, tracking, or advertising.
- It does **not** sell, rent, or share your data with anyone.

## Google API Services — Limited Use disclosure

OpenFit's use and transfer of information received from Google APIs adheres to
the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the **Limited Use** requirements. Specifically, information obtained
from the Google Health API is:

- used only to provide and improve the user-facing features of the OpenFit
  instance the data owner is running;
- not transferred to others except as necessary to provide those features,
  to comply with applicable law, or as part of a merger/acquisition with the
  data owner's consent;
- not used for advertising; and
- not read by any human except the person who operates the instance (the data
  owner), except where required for security, to comply with applicable law, or
  with that person's explicit consent.

## Retention, revocation, and deletion

You control retention entirely, because the data lives on your own instance.
Within OpenFit you can remove a connected source at any time, which deletes its
stored credentials and (optionally) wipes the data collected from it. You can
also revoke OpenFit's access to your Google account at any time from your
[Google Account permissions page](https://myaccount.google.com/permissions).

## Contact

This instance is operated by: **Your Name <you@example.com>**.
For questions about the OpenFit software itself, see the project repository:
**https://github.com/Raven-git-hub/OpenFit**.

## Changes to this policy

This policy may be updated as OpenFit evolves. Material changes will be noted in
the project repository.
