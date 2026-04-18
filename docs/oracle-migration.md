# Migrating to Oracle Cloud Free Tier

Oracle Cloud Always Free gives you 2 ARM VMs (1 OCPU, 1GB RAM each) that never expire and require no credit card tricks.

## 1. Create an Oracle Cloud account

Sign up at cloud.oracle.com. Choose a home region close to you (can't be changed later).

## 2. Provision a VM

1. Go to **Compute → Instances → Create Instance**
2. Change shape to **Ampere (ARM)** → `VM.Standard.A1.Flex` → 1 OCPU, 1GB RAM
3. Under **Networking**, ensure a public IP is assigned
4. Add your SSH public key (`~/.ssh/id_rsa.pub`)
5. Click **Create**

## 3. Open firewall (if needed)

For a long-polling bot no inbound ports are needed — skip this.

## 4. SSH in and install dependencies

```bash
ssh ubuntu@<your-instance-ip>

# Update and install Docker
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
# Log out and back in for group change to take effect
```

## 5. Clone the repo and configure

```bash
git clone https://github.com/guidodinello/knowledger-bot.git
cd knowledger-bot
cp .env.example .env
nano .env   # fill in the three variables
```

## 6. Run the bot

```bash
docker build -t knowledger .
docker run -d --name knowledger --restart unless-stopped --env-file .env knowledger
```

Check logs:
```bash
docker logs -f knowledger
```

## 7. Updating the Claude session token

When the token expires, SSH in and run:

```bash
cd knowledger-bot
nano .env   # update CLAUDE_SESSION_TOKEN
docker restart knowledger
```

## 8. Day-2 operations (deploy script)

A `deploy.sh` script at the project root handles common operations from your local machine:

```bash
./deploy.sh env      # push .env.oracle to server and recreate container
./deploy.sh update   # git pull on server, rebuild image, recreate container
./deploy.sh logs     # tail container logs
./deploy.sh restart  # recreate container (e.g. after manual server edits)
```

Keep `.env.oracle` locally, edit it there, and run `./deploy.sh env` to push.

> **Note:** `docker restart` is not enough for env changes — it reuses the container's
> original environment. You must `rm -f` the container and recreate it to pick up new
> values from `--env-file`.

## 9. Troubleshooting

### A1.Flex out of capacity

São Paulo has only one availability domain, so there is no alternative AD to try.
Fall back to **VM.Standard.E2.1.Micro** (AMD, x86_64) — it is also Always Free and
always has capacity. The rest of the guide is identical.

### Public IP not assignable during instance creation

If the "Assign public IPv4" toggle is greyed out with a warning about needing a public
subnet, skip it and assign the IP post-creation:

1. Instance page → **Networking** tab → click the VNIC
2. **IP Addresses** tab → three-dot menu next to the private IP → **Edit**
3. Select **Ephemeral public IP** → Save

### `.env` values must not be quoted

Docker's `--env-file` parser passes values literally — surrounding quotes become part of
the string. Unlike `python-dotenv` (which strips them), Docker will pass `"8713385393"`
as-is, causing `int()` conversion to fail.

Keep values unquoted in `.env.oracle`:

```
ALLOWED_USER_IDS=123456789    # correct
ALLOWED_USER_IDS="123456789"  # wrong — quotes included in value
```
