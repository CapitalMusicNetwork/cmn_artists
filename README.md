# CMN Artist Portal

Developed for [Capital Music Network](https://capitalmusicnetwork.com), a streaming
service that lets local artists upload music and supporters subscribe to access
and listen to the music.

All of the streaming is handled by [Navidrome](https://www.navidrome.org/), which
is hosted separately from this app.

This app contains all of the non-streaming features:

- Users can manage their accounts: create accounts, reset passwords
- Users can purchase streaming subscriptions (via Stripe Checkout), and manage/cancel their existing subscriptions
- Users can apply to be included as artists in the platform. If approved, they
can upload their music and manage the metadata (ie provide album art, spelling and formatting for track names)
- Administrators can review and approve artist applications, manage user subscriptions, and access revenue reports 

## Architecture

### Requirements

See `requirements.txt`. This is a straightforwad django app, but it uses:

- **django-allauth** - Handles email verification, password resets, etc
- **Stripe** - subscription billing and webhooks
- **Navidrome's API** - to sync accounts between this app and the Navidrome server. See below for more details.
- **mutagen** - audio file tag reading/writing

### Navidrome API Integration

#### Music

This doesn't actually use the API - we just have a folder that is shared between this app and Navidrome. This app places files into that folder and Navidrome automatically picks them up.

#### Accounts

This is a brief summary. See `accounts/navidrome.py` for implementation details.

The goal is that users should only interact with our app to manage their Navidrome account. When a user verifies their email and logs in for the first time, they automatically get an account with the same username/password in Navidrome. (It waits for verification so bot signups that never confirm their email don't get Navidrome logins.) This happens via a [Django AllAuth AccountAdapter](https://docs.allauth.org/en/latest/account/adapter.html) (see `subscribers.adapter.NavidromeAccountAdapter`) which captures the plaintext password as the user logs in and sends it to the Navidrome API. Note that Django and Navidrome have their own separate hashing schemes so the passwords won't match in the database, but their plaintext will be the same. This also means that there's no way to 're-sync' the passwords if they get out of sync - there's no way to take the hashed password from Django, unhash it, and send it to Navidrome. A password reset works via the Django app because it gets the new password in plaintext and sends to Navidrome.

#### Subscriptions

All users have accounts on Navidrome, and subscribing/unsubscribing doesn't impact the ability to login. Instead, we use [Navidrome libraries](https://www.navidrome.org/docs/usage/features/multi-library/). We have two libraries - a sample library that everyone can use to test out the functionality, and the full library with all of the real music. All users are permissioned automatically to the sample library (this happens automatically because it's marked as "default" in Navidrome settings). Access to the full library is added/removed based on their subscription status.


### Database

DB schema diagrams are in the `schema/` directory - there are two versions, one that focuses on the data unique to this project, and a full schema that includes a bunch of django boilerplate tables. Both are generated via [django-extensions](https://github.com/django-extensions/django-extensions).

To regenerate them (requires `graphviz`: `sudo apt install graphviz`):

```sh
python manage.py graph_models accounts account subscribers music -g -o schema/schema.png
python manage.py graph_models -a -g -o schema/full_schema.png
```
### Environment variables

There are a ton of environment variables you need to set in order to connect to Navidrome, Stripe, an email provider, and the database. See `.env.example` (which works for the local django testing server) or `docker-compose-example.yml` (for production deployment) for a complete list.

### Production deployment

A `Dockerfile` and `docker-compose-example.yml` are included. The Dockerfile runs `entrypoint.sh` which runs migrations, collects static files, then starts Gunicorn.

Copy `docker-compose-example.yml` to `docker-compose.yml`, fill in the environment variables, and run:

```sh
docker compose up -d
```

When artists upload media files (audio, album art), they are stored in a directory that should be presented to the container as a volume mounted at `/media`. On the production server, Navidrome should point its music library at the same path so it can automatically pick up uploaded tracks.

### Local Development

For development, I'm connecting to the production Navidrome instance (setting up a testing Navidrome instance is a big todo!), a Stripe sandbox API key, and the Stripe CLI: 

```sh
./stripe login
./stripe listen --forward-to localhost:8000/stripe/webhook/
```

From there, we can use any of Stripe's test credit cards to place fake payments: <https://docs.stripe.com/testing>

### Signup bot protection

The signup form has a hidden honeypot field and a self-hosted [ALTCHA](https://altcha.org) proof-of-work check: no third-party service is involved. Challenges are served from `/accounts/altcha-challenge/` and signed with a key derived from `SECRET_KEY` (see `subscribers/signup_protection.py`); the widget is vendored in `static/vendor/altcha/`. Blocked attempts are logged as `Blocked signup for ...` warnings.

Accounts that never verify their email are deleted after 7 days by the scheduler (`python manage.py delete_unverified_accounts`, see `--help`).

## Backups

Everything is backed up to Backblaze. See `backup/README.md` for more details.