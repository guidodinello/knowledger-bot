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

## 8. Updating the bot (after a git push)

```bash
cd knowledger-bot
git pull
docker build -t knowledger .
docker rm -f knowledger
docker run -d --name knowledger --restart unless-stopped --env-file .env knowledger
```
