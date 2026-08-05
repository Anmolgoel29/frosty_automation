# Docker Setup Guide - OpenOutreach

Complete guide to deploy OpenOutreach with Docker in production.

---

## 📋 Prerequisites

- Docker 20.10+
- Docker Compose (optional but recommended)
- 4GB+ RAM
- 20GB+ disk space
- Available ports: 5902, 6082, 8002

---

## 🚀 Quick Start (5 minutes)

### 1. Clone & Navigate

```bash
cd /home/outreach
git clone https://github.com/eracle/OpenOutreach.git
cd OpenOutreach
```

### 2. Remove Private Dependencies

```bash
# Remove openoutreach package dependency
sed -i '/openoutreach @ git+https/d' requirements/base.txt
```

### 3. Create Configuration File

```bash
# Create data directory
mkdir -p /home/outreach/openoutreach_data

# Create config.json with your settings
cat > /home/outreach/openoutreach_data/config.json << 'EOF'
{
  "linkedin_email": "your.email@gmail.com",
  "linkedin_password": "your_secure_password",
  "campaign_name": "B2B Lead Generation",
  "product_description": "Your product description here",
  "campaign_objective": "Your target market and goals",
  "booking_link": "https://calendly.com/your-link",
  "seed_urls": "",
  "chat_llm_provider": "anthropic",
  "chat_llm_api_key": "your_chat_api_key_here",
  "chat_ai_model": "claude-sonnet-5",
  "chat_llm_api_base": "",
  "task_llm_provider": "groq",
  "task_llm_api_key": "your_task_api_key_here",
  "task_ai_model": "mixtral-8x7b-32768",
  "task_llm_api_base": "",
  "newsletter": false,
  "connect_daily_limit": 10,
  "connect_weekly_limit": 50,
  "follow_up_daily_limit": 5,
  "legal_acceptance": true
}
EOF

# Verify JSON is valid
python3 -m json.tool /home/outreach/openoutreach_data/config.json > /dev/null && echo "✅ Config valid"
```

### 4. Build Docker Image

```bash
# From inside OpenOutreach directory
docker build --no-cache \
  -f compose/linkedin/Dockerfile \
  --build-arg BUILD_ENV=production \
  -t openoutreach:latest .

# Verify build succeeded
docker images | grep openoutreach
```

### 5. Run Container (Production - Background)

```bash
# Stop any existing container
docker stop openoutreach 2>/dev/null
docker rm openoutreach 2>/dev/null

# Run in daemon mode (background)
docker run -d \
  --name openoutreach \
  -p 5902:5900 \
  -p 6082:6080 \
  -p 8002:8000 \
  -v /home/outreach/openoutreach_data:/app/data \
  -e ENABLE_VNC=true \
  --restart unless-stopped \
  openoutreach:latest

# Verify running
docker ps | grep openoutreach
```

### 6. Create Django Superuser

```bash
# Create admin account
docker exec -it openoutreach python manage.py createsuperuser

# Prompts:
# Username: admin
# Email: admin@example.com
# Password: your_strong_password
```

---

## 🖥️ Access Services

### Web Interfaces

```
Django Admin:  http://<VPS_IP>:8002/admin/
Web VNC:       http://<VPS_IP>:6082/vnc.html
```

### Native VNC Client

```
Host:     <VPS_IP>
Port:     5902
Password: (empty - none)
```

### Via SSH Tunnel (Secure)

```bash
# From local machine
ssh -L 5902:localhost:5902 \
    -L 6082:localhost:6082 \
    -L 8002:localhost:8002 \
    root@<VPS_IP> -N

# Then access locally:
# http://localhost:6082/vnc.html
# http://localhost:8002/admin/
```

### Via Cloudflare Tunnel

```bash
# Start tunnel (on VPS)
cloudflared tunnel run openoutreach

# Access via domain:
# https://openout.solveease.in
# https://vnc.openout.solveease.in
```

---

## 🔧 Container Management

```bash
# View logs
docker logs -f openoutreach

# Enter container shell
docker exec -it openoutreach bash

# Restart container
docker restart openoutreach

# Stop container
docker stop openoutreach

# Start container
docker start openoutreach

# Remove container (data persists)
docker rm openoutreach

# Full reset (WARNING: deletes data)
docker rm openoutreach
rm -rf /home/outreach/openoutreach_data
```

---

## ⚙️ Django Admin Tasks

```bash
# Run migrations
docker exec openoutreach python manage.py migrate

# Create superuser
docker exec -it openoutreach python manage.py createsuperuser

# Django shell
docker exec -it openoutreach python manage.py shell

# Check status
docker exec openoutreach python manage.py shell << 'EOF'
from linkedin.models import Campaign, LinkedInProfile, SiteConfig
print(f"Campaign: {Campaign.objects.first()}")
print(f"Account: {LinkedInProfile.objects.first()}")
cfg = SiteConfig.load()
print(f"Chat LLM: {cfg.chat_ai_model}  Task LLM: {cfg.task_ai_model}")
EOF
```

---

## 📁 Data & Configuration

### Data Location (Host)
```
/home/outreach/openoutreach_data/
├── db.sqlite3           # Main database
├── config.json          # Configuration
└── other files...
```

### Update Configuration

```bash
# 1. Edit config file
nano /home/outreach/openoutreach_data/config.json

# 2. Restart container to apply changes
docker restart openoutreach

# Or edit directly in Django Admin:
# http://<VPS_IP>:8002/admin/
```

### Backup Data

```bash
# Backup everything
cp -r /home/outreach/openoutreach_data \
  /home/outreach/backup-$(date +%Y%m%d-%H%M%S)

# Or with compression
tar -czf openoutreach-backup-$(date +%Y%m%d).tar.gz \
  /home/outreach/openoutreach_data
```

---

## 🐛 Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs openoutreach

# Run interactively to see errors
docker run -it \
  --name openoutreach_debug \
  -p 5902:5900 \
  -p 6082:6080 \
  -p 8002:8000 \
  -v /home/outreach/openoutreach_data:/app/data \
  openoutreach:latest

# Press Ctrl+C to stop
```

### Port Already in Use

```bash
# Find what's using port
netstat -tuln | grep 5902
netstat -tuln | grep 6082
netstat -tuln | grep 8002

# Kill process if needed
sudo fuser -k 5902/tcp
```

### Config Not Loading

```bash
# Verify config file exists
ls -la /home/outreach/openoutreach_data/config.json

# Check it's valid JSON
python3 -m json.tool /home/outreach/openoutreach_data/config.json

# Verify container can see it
docker exec openoutreach ls -la /app/data/config.json
```

### Database Issues

```bash
# Reset database (WARNING: deletes all data)
rm /home/outreach/openoutreach_data/db.sqlite3

# Restart container (will recreate DB)
docker restart openoutreach

# Recreate superuser
docker exec -it openoutreach python manage.py createsuperuser
```

---

## 🔗 Port Configuration

If ports are occupied, modify the run command:

```bash
docker run -d \
  --name openoutreach \
  -p 5903:5900 \      # Change first number (host port)
  -p 6083:6080 \
  -p 8003:8000 \
  -v /home/outreach/openoutreach_data:/app/data \
  -e ENABLE_VNC=true \
  --restart unless-stopped \
  openoutreach:latest

# Then access:
# http://<VPS_IP>:6083/vnc.html
# http://<VPS_IP>:8003/admin/
```

---

## 💾 Docker Compose (Alternative)

### Create docker-compose.yml

```yaml
# filepath: /home/outreach/docker-compose.openoutreach.yml
version: '3.9'

services:
  openoutreach:
    image: openoutreach:latest
    container_name: openoutreach
    ports:
      - "5902:5900"
      - "6082:6080"
      - "8002:8000"
    volumes:
      - openoutreach_data:/app/data
    environment:
      - ENABLE_VNC=true
    restart: unless-stopped

volumes:
  openoutreach_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /home/outreach/openoutreach_data
```

### Using Compose

```bash
# Start
docker-compose -f docker-compose.openoutreach.yml up -d

# Stop
docker-compose -f docker-compose.openoutreach.yml down

# Logs
docker-compose -f docker-compose.openoutreach.yml logs -f

# Remove volumes (data)
docker-compose -f docker-compose.openoutreach.yml down -v
```

---

## 📊 Configuration Reference

### LinkedIn Settings (Edit in Django Admin)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `connect_daily_limit` | int | 10 | Connections per day |
| `connect_weekly_limit` | int | 50 | Connections per week |
| `follow_up_daily_limit` | int | 5 | Follow-up messages per day |

### LLM Providers

```
groq              # Free tier available
openai            # GPT-4, GPT-4o
anthropic         # Claude
google            # Gemini (free)
mistral           # Mistral
cohere            # Cohere
bedrock           # AWS Bedrock
```

### Rate Limiting Best Practices

```
Week 1:  5 connections/day   (Warm-up)
Week 2:  10 connections/day  (If no warnings)
Week 3+: 20-30/day           (If still no warnings)
```

---

## 🔐 Security Notes

- Store credentials securely (use config.json in secure location)
- Use dedicated LinkedIn account (not personal)
- Enable 2FA on LinkedIn account
- Change default passwords
- Use SSH tunnels for remote access
- Keep Docker image updated

---

## 📈 Monitoring

```bash
# Check container resource usage
docker stats openoutreach

# View recent logs
docker logs openoutreach --tail 50

# Monitor in real-time
watch -n 1 'docker ps | grep openoutreach'
```

---

## 🎯 Complete One-Liner Setup

```bash
cd /home/outreach/OpenOutreach && \
sed -i '/openoutreach @ git+https/d' requirements/base.txt && \
mkdir -p /home/outreach/openoutreach_data && \
cat > /home/outreach/openoutreach_data/config.json << 'EOF'
{
  "linkedin_email": "your.email@gmail.com",
  "linkedin_password": "your_password",
  "campaign_name": "Lead Generation",
  "product_description": "Your product",
  "campaign_objective": "Your objective",
  "booking_link": "https://calendly.com/link",
  "seed_urls": "",
  "chat_llm_provider": "anthropic",
  "chat_llm_api_key": "your_chat_key",
  "chat_ai_model": "claude-sonnet-5",
  "chat_llm_api_base": "",
  "task_llm_provider": "groq",
  "task_llm_api_key": "your_task_key",
  "task_ai_model": "mixtral-8x7b-32768",
  "task_llm_api_base": "",
  "newsletter": false,
  "connect_daily_limit": 10,
  "connect_weekly_limit": 50,
  "follow_up_daily_limit": 5,
  "legal_acceptance": true
}
EOF
python3 -m json.tool /home/outreach/openoutreach_data/config.json > /dev/null && \
docker build --no-cache -f compose/linkedin/Dockerfile --build-arg BUILD_ENV=production -t openoutreach:latest . && \
docker rm -f openoutreach 2>/dev/null; \
docker run -d --name openoutreach -p 5902:5900 -p 6082:6080 -p 8002:8000 \
  -v /home/outreach/openoutreach_data:/app/data \
  -e ENABLE_VNC=true --restart unless-stopped openoutreach:latest && \
sleep 3 && docker logs openoutreach
```

---

## ✅ Deployment Checklist

- [ ] Docker installed and running
- [ ] OpenOutreach repo cloned
- [ ] Private dependency removed from requirements
- [ ] config.json created with valid settings
- [ ] Docker image built successfully
- [ ] Container running in daemon mode
- [ ] Django superuser created
- [ ] Can access http://<VPS_IP>:6082/vnc.html
- [ ] Can access http://<VPS_IP>:8002/admin/
- [ ] Container auto-restarts on reboot
- [ ] Data backed up

---

## 📞 Quick Reference

```bash
# Get VPS IP
hostname -I

# Check container status
docker ps | grep openoutreach

# View logs live
docker logs -f openoutreach

# Access admin
docker exec -it openoutreach python manage.py createsuperuser

# Full restart
docker restart openoutreach

# Emergency stop
docker stop openoutreach

# Clean full reset
docker rm openoutreach && rm -rf /home/outreach/openoutreach_data
```

---

**Your OpenOutreach Docker setup is complete and ready for production!** 🚀
