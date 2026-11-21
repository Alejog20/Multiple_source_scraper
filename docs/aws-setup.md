# AWS setup — manual steps for the EC2 deploy

This is a guide for what **you** do in the AWS Console/CLI. Nothing here
is applied automatically — no Terraform/CloudFormation is included,
deliberately, since provisioning account-level IAM resources isn't
something to run unattended from a coding session. Follow it top to
bottom; later steps depend on earlier ones.

## 1. EC2 instance

- **Type**: `t4g.small` (ARM64, Graviton — cheaper than an equivalent
  x86 instance and the Dockerfile builds for `linux/arm64`).
- **AMI**: an arm64 AMI — Amazon Linux 2023 or Ubuntu 24.04, either works;
  the steps below note where the two diverge.
- **EBS**: 20 GB `gp3`, **encrypted** (tick the box at launch — free,
  no reason not to).
- **Metadata options**: set **IMDSv2 required** (not just "available").
  This is what `scripts/probe_amazon.py` and `/diag`'s EC2 detection
  check for, and it closes the SSRF-to-credential-theft path that IMDSv1
  is known for.
- **Security Group**: **no inbound rules at all**. You will never SSH
  into this box — deploys go through SSM Run Command (see §3), and the
  bot only makes outbound connections (Telegram, Amazon, MercadoLibre).
  Outbound: leave the default (all traffic allowed).
- **Key pair**: none needed, for the same reason — no SSH.

## 2. IAM instance role

Create a role for the EC2 instance itself (not the GitHub Actions role —
that's §3) with:

- **`AmazonSSMManagedInstanceCore`** (AWS managed policy) — lets SSM
  Agent on the instance receive Run Command calls.
- An inline policy scoped to backups only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::<bucket-backups>/scraper/*"
    }
  ]
}
```

Attach this role to the instance (Launch Instance -> Advanced ->
IAM instance profile, or `aws ec2 associate-iam-instance-profile` after
the fact).

## 3. IAM role for GitHub OIDC (used by `deploy-ec2.yml`)

This is what lets the `Deploy to EC2` workflow authenticate to AWS
**without** storing a long-lived access key in GitHub Secrets.

1. One-time per AWS account: add GitHub's OIDC provider if it isn't
   already there (`token.actions.githubusercontent.com`, thumbprint
   managed by AWS — the console form has the right defaults).
2. Create a role with this trust policy, restricted to this repo's
   `master` branch specifically (not just the whole repo — a PR branch
   should never be able to assume a deploy role):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:Alejog20/Multiple_source_scraper:ref:refs/heads/master"
        }
      }
    }
  ]
}
```

3. Permissions policy — only what `deploy-ec2.yml` actually calls,
   scoped to this one instance and this one SSM document:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ssm:SendCommand"],
      "Resource": [
        "arn:aws:ec2:us-east-1:<ACCOUNT_ID>:instance/<EC2_INSTANCE_ID>",
        "arn:aws:ssm:us-east-1::document/AWS-RunShellScript"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation"],
      "Resource": "*"
    }
  ]
}
```

   (`GetCommandInvocation` doesn't support resource-level restriction —
   that's an AWS limitation, not a shortcut taken here.)

4. In the GitHub repo: **Settings -> Secrets and variables -> Actions**:
   - Secret `AWS_DEPLOY_ROLE_ARN` = the role's ARN from step 2.
   - Secret `EC2_INSTANCE_ID` = `i-xxxxxxxxxxxxxxxxx`.
   - Variable `DEPLOY_ENABLED` = `true` (this is what turns the
     `deploy-ec2.yml` job from "skipped" to "runs" — leave it unset/false
     until you've finished steps 1-2 above and the instance exists).

## 4. On the instance itself

SSH is unavailable by design — use **Session Manager** instead:
`aws ssm start-session --target <EC2_INSTANCE_ID>`.

```bash
# Amazon Linux 2023:
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user   # or ssm-user
# Ubuntu 24.04:
sudo apt update && sudo apt install -y docker.io docker-compose-plugin

# Both: 2GB swap (t4g.small has 2GB RAM — Chromium + the bot process can
# get tight without it)
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Automatic security updates:
#   Amazon Linux 2023: sudo dnf install -y dnf-automatic && sudo systemctl enable --now dnf-automatic.timer
#   Ubuntu 24.04:      sudo apt install -y unattended-upgrades

sudo mkdir -p /opt/scraper-bot
sudo chown $USER /opt/scraper-bot
git clone https://github.com/Alejog20/Multiple_source_scraper.git /opt/scraper-bot
cd /opt/scraper-bot
cp .env.example .env
chmod 600 .env
# edit .env with real values: TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS,
# ADMIN_USER_ID, etc. — see docs/amazon-access-test.md for HTTPS_PROXY_AMAZON

mkdir -p data
sudo chown -R 10001:10001 data   # the container runs as UID 10001, not root

docker compose build
docker compose up -d
```

After this, `deploy-ec2.yml` takes over: every successful `Tests` run on
`master` re-runs `git reset --hard` + `docker compose up -d --build` on
this same checkout via SSM.

## 5. Daily DB backup to S3

- Create the S3 bucket (`<bucket-backups>` above) with a lifecycle rule:
  expire objects under `scraper/` after 30 days.
- Copy `scripts/backup_db.sh` to the instance (it's already there if you
  cloned the repo into `/opt/scraper-bot` per §4).
- Install a systemd timer:

```ini
# /etc/systemd/system/scraper-backup.service
[Unit]
Description=Backup scraper_jobs.db to S3

[Service]
Type=oneshot
Environment=S3_BUCKET=<bucket-backups>
Environment=DB_PATH=/opt/scraper-bot/data/scraper_jobs.db
ExecStart=/opt/scraper-bot/scripts/backup_db.sh
```

```ini
# /etc/systemd/system/scraper-backup.timer
[Unit]
Description=Daily scraper_jobs.db backup

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now scraper-backup.timer
sudo systemctl start scraper-backup.service   # test it once immediately
```

## 6. Cost control

Create an **AWS Budget** (Billing -> Budgets) for this account/tag with
two alert thresholds: **$5** and **$10** per month, emailed to you. A
`t4g.small` running 24/7 plus a 20GB gp3 volume and S3 backups should sit
well under $10/mo, so either alert firing means something unexpected is
running (a leaked EBS snapshot, a forgotten Elastic IP, etc.).

## 7. Optional: SSM Parameter Store for the bot token

Instead of a plaintext `.env` on disk, you can store
`TELEGRAM_BOT_TOKEN` as a `SecureString` in SSM Parameter Store and have
the deploy script fetch it into `.env` at deploy time:

```bash
aws ssm put-parameter --name /scraper-bot/telegram-token \
  --type SecureString --value "<token>"
```

Then extend the SSM Run Command script in `deploy-ec2.yml` (or a
wrapper script on the instance) to run:

```bash
aws ssm get-parameter --name /scraper-bot/telegram-token --with-decryption \
  --query Parameter.Value --output text
```

and write it into `.env` before `docker compose up -d`. This is optional
— a `chmod 600 .env` file on a box with no inbound access and no SSH is
already a reasonable bar; only do this if you want the token to never
touch disk in plaintext at all.
