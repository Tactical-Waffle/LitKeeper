# Deploying "litkeeparr" on your Docker server

litkeeparr is LitKeeper plus the extra features on the `litkeeparr` branch: the Settings → Notifications
page and AI covers. The folders inside the container are named `/litkeeparr` instead of `/litkeeper`.

**How it gets to your server:** every time the `litkeeparr` branch is pushed to GitHub, GitHub builds a
fresh image and publishes it as:

```
ghcr.io/tactical-waffle/litkeeparr:latest
```

Your server just downloads that image, the same way you'd install Sonarr. You can follow each build in
the fork's **Actions** tab: https://github.com/Tactical-Waffle/LitKeeper/actions

**Before you start, replace these placeholders in the commands:**
- `you@your-server`: how you normally connect to the server with `ssh`, for example `chris@192.168.1.20`.
- Port `5000`: change the left-hand number in step 2 if something on the server already uses port 5000.

The commands assume you can reach the server with `ssh` and it has Docker with
`docker compose`. If you manage it through a web panel (Portainer, Unraid, Synology and so on),
create a stack from the compose file in step 2 instead.

---

## 0. One-time GitHub setup

1. **Let the image be downloaded without signing in.** GitHub may make a new image private. After the
   first build finishes, open https://github.com/Tactical-Waffle?tab=packages, choose **litkeeparr**,
   then **Package settings → Change visibility → Public**. The code is already public, so this
   doesn't reveal anything new.

   *Prefer to keep it private?* Make a GitHub token with only the `read:packages` permission
   (GitHub → Settings → Developer settings → Personal access tokens), then sign the server in once:
   ```bash
   docker login ghcr.io -u Tactical-Waffle
   ```
   and paste the token when it asks for a password.

## 1. Make the folders (on the server)

Log in to the server:

```bash
ssh you@your-server
```

**The rest of the commands run on the server.**

```bash
mkdir -p ~/litkeeparr/data ~/litkeeparr/stories
```

## 2. Create the compose file

```bash
nano ~/litkeeparr/docker-compose.yml
```

Paste this in, then save with **Ctrl+O**, **Enter**, **Ctrl+X**:

```yaml
services:
  litkeeparr:
    image: ghcr.io/tactical-waffle/litkeeparr:latest
    container_name: litkeeparr
    restart: unless-stopped
    ports:
      - "5000:5000"              # change the LEFT number if port 5000 is taken
    volumes:
      - ./data:/litkeeparr/app/data        # database and secret key
      - ./stories:/litkeeparr/app/stories  # EPUBs, reader files, covers
    environment:
      - ENABLE_LIBRARY=true
```

The right-hand side of each volume line must say `/litkeeparr/...`, to match the folder names inside
this image. The official LitKeeper image uses `/litkeeper/...` instead.

Notifications and AI covers don't need anything in this file. You set them up in the app afterwards.

## 3. Start it

```bash
cd ~/litkeeparr && docker compose pull && docker compose up -d
```

Watch it start up:

```bash
docker compose logs -f
```

Wait until you see `Starting application...` and the Gunicorn lines, then press **Ctrl+C** to stop
watching. The app keeps running.

## 4. Open it and set things up

Go to `http://<server-ip>:5000` in your browser.

**Notifications:** Settings → Notifications → Add Notification:
1. Choose **Pushover**.
2. Paste your **User Key** and **API Token** (your application's token from pushover.net).
3. Press **Test**. A message should arrive on your phone.
4. Press **Save**.

**AI covers (optional):** Settings → AI Covers. Paste your OpenRouter key, press **Check Key & Models**,
then try **AI Cover** on one story. Each cover costs a few cents of OpenRouter credit.

---

## Behind SWAG (reverse proxy)

If you reach LitKeeper through SWAG at an address like `litkeeper.<your-domain>`:
1. Leave out the `ports:` lines. SWAG reaches the container over the Docker network, so nothing
   needs to be open on the server, and port 5000 stays free for other apps.
2. Copy [deploy/swag/litkeeparr.subdomain.conf.sample](deploy/swag/litkeeparr.subdomain.conf.sample) to
   SWAG's `/config/nginx/proxy-confs/litkeeparr.subdomain.conf`, delete any old
   `litkeeper.subdomain.conf`, then restart SWAG.
3. Keep the same address as before. Passkeys (the lock in Settings) are tied to the address, so
   changing it means setting them up again.

## Updating litkeeparr later

Once a new build has finished in the Actions tab, run this on the server:

```bash
cd ~/litkeeparr && docker compose pull && docker compose up -d
```

Your `data` and `stories` folders aren't touched by this.

**Automatic updates (optional):** a tool like Watchtower can check for a new image and restart the
container for you, so you never have to run the command above.

**Going back to an older build:** every build is also tagged with its short commit id (for example
`ghcr.io/tactical-waffle/litkeeparr:0f0cb95`). Put that tag in the `image:` line instead of `latest`.

## Already running the official LitKeeper on this server?

You can bring your existing library across. Don't run both on the same data folders at the same
time: they share one database file, and two apps writing to it at once can damage it.

1. Stop the old one (in its own folder): `docker compose down`
2. Copy its data into the new folders. Replace `/path/to/old` with the folder holding its
   `data` and `stories` folders:
   ```bash
   cp -a /path/to/old/data/. ~/litkeeparr/data/
   ```
   ```bash
   cp -a /path/to/old/stories/. ~/litkeeparr/stories/
   ```
3. Start litkeeparr (step 3). The old folders stay untouched as a backup.

These changes don't alter the database layout, so you can switch back to the official image later
with the same data.

## Going back to the official LitKeeper

In `~/litkeeparr/docker-compose.yml`:
1. Change the image line to `image: ghcr.io/redwoodstory/litkeeper:latest`.
2. Change `/litkeeparr/` back to `/litkeeper/` in both volume lines. The official image uses the old folder names.

Then run:

```bash
cd ~/litkeeparr && docker compose pull && docker compose up -d
```

Your library and settings stay. Saved notification entries and AI cover settings stay in the database
but aren't used by the official version.

## Building it yourself instead (without GitHub)

If you ever want to build on the server from the code directly:

```bash
git clone -b litkeeparr https://github.com/Tactical-Waffle/LitKeeper.git ~/litkeeparr/src
```

Then in the compose file, add `build: ./src` above the `image:` line and change the image to
`litkeeparr:latest`, and start it with `docker compose up -d --build`. To update, run `git pull` in
`~/litkeeparr/src` and the same `docker compose up -d --build`.
