# OpenOutreach Docker Installation & Setup Guide

## 🎯 What is OpenOutreach?

OpenOutreach is a **self-hosted, open-source LinkedIn automation tool** for B2B lead generation without any contact lists needed. It:

- **Autonomously discovers leads** — AI generates LinkedIn search queries, finds prospects matching your target market
- **Qualifies leads intelligently** — Uses Bayesian ML model (Gaussian Process Regressor) to learn which profiles match your ideal customer  
- **Auto-contacts prospects** — Sends personalized connection requests to qualified leads
- **Manages conversations** — AI-powered follow-up agent handles multi-turn conversations
- **Works locally** — Self-hosted on Docker with full data ownership (no cloud lock-in)
- **Mimics real behavior** — Playwright + stealth plugins avoid detection and account bans
- **GUI + VNC** — Watch automation happen in real-time via browser-based VNC viewer

### Key Components:
- **Django CRM** — Web admin interface to manage leads, deals, campaigns
- **Playwright Browser** — Autonomous LinkedIn browser automation
- **ML Pipeline** — Gaussian Process learns your ideal customer profile  
- **LLM Integration** — Bring your own API key (OpenAI, Anthropic, Groq, Mistral, etc.)
- **VNC Server** — Live GUI to watch browser actions

---

## ⚙️ System Requirements

### Hardware:
- **CPU**: 2+ cores (4+ recommended for smooth operation)
- **RAM**: 4GB minimum (8GB recommended)
- **Storage**: 20GB+ free space (for browser cache, database, embeddings)
- **Network**: Stable internet connection

### Software:
- **Docker**: 20.10+ (install from https://docs.docker.com/get-docker/)
- **Docker Compose**: 1.29+ (usually included with Docker Desktop)
- **VNC Client** (optional but recommended):
  - Linux: `vinagre`, `vncviewer`, `krdc`
  - macOS: Built-in "Screen Sharing" or `OmniGraffle`
  - Windows: `RealVNC`, `TightVNC`, or browser-based noVNC (built-in)

### Credentials You'll Need:
1. **LinkedIn Account** — Email + password
2. **LLM API Key** — OpenAI, Anthropic, Groq, Mistral, etc.
3. **LinkedIn Campaign Details** — Product description + target market

---

## 📍 Installation Methods

### Method 1: Quick Docker Run (Easiest - Recommended)

```bash
# Create a data directory on your host
mkdir -p ~/.openoutreach/data

# Run the latest pre-built image
docker run --pull always -it \
  -p 5900:5900 \
  -p 6080:6080 \
  -v ~/.openoutreach/data:/app/data \
  ghcr.io/eracle/openoutreach:latest
```

**What this does:**
- `-p 5900:5900` — VNC server (native VNC client)
- `-p 6080:6080` — noVNC web viewer (browser-based)
- `-v ~/.openoutreach/data:/app/data` — Persistent storage for DB, cookies, models
- `--pull always` — Always fetch latest image from GitHub Container Registry

**First run:**
- Interactive onboarding wizard will prompt for:
  - LinkedIn email & password
  - LLM provider & API key
  - Campaign name, product description, target market
  - Booking link (optional)

---

### Method 2: Docker Compose (Development/Customization)

```bash
# Clone the repository
git clone https://github.com/eracle/OpenOutreach.git
cd OpenOutreach

# Set user permissions (if your UID is not 1000)
export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

# Build & start the container
make up

# Follow logs
make logs
```

**The Makefile includes:**
- `make up` — Build and start with live logs
- `make stop` — Stop the service
- `make build` — Just build the image
- `make logs` — Tail application logs
- `make docker-test` — Run tests

**Data persists** in `OpenOutreach` directory mounted at `/app` inside the container.

---

## 🖥️ Accessing the GUI & Monitoring

### Option 1: Browser-based VNC (Easiest - No Client Install)

**URL:** http://localhost:6080/vnc.html

Open this in your browser. You'll see:
- Desktop with browser automation live
- Firefox/Chromium performing LinkedIn actions in real-time
- Terminal logs

### Option 2: Native VNC Client

**Connection Details:**
- Host: `localhost`
- Port: `5900`
- Password: None (empty)

**Linux:**
```bash
vinagre vnc://localhost:5900
```

**macOS:**
```bash
open vnc://localhost:5900
```

**Windows:**
Download RealVNC Viewer or TightVNC and connect to `localhost:5900`

### Option 3: Django Admin CRM Interface

After first run, create a superuser:

```bash
# Enter the running container
docker exec -it <container-id> bash

# Create superuser
python manage.py createsuperuser
```

Then open: **http://localhost:8000/admin/**

In Django Admin you can:
- View/manage Leads and Deals
- Monitor campaign progress
- Adjust rate limits
- Check conversation history

---

## ⚙️ Step-by-Step Setup Process

### Step 1: Start the Container

```bash
mkdir -p ~/.openoutreach/data

docker run --pull always -it \
  -p 5900:5900 \
  -p 6080:6080 \
  -v ~/.openoutreach/data:/app/data \
  ghcr.io/eracle/openoutreach:latest
```

### Step 2: Interactive Onboarding (Automatic)

The daemon will prompt you for:

#### A. LinkedIn Credentials
```
LinkedIn Email: your.email@gmail.com
LinkedIn Password: ••••••••
```

#### B. LLM Configuration
Choose your provider:
- **OpenAI** (GPT-4, GPT-4o) — https://platform.openai.com/api-keys
- **Anthropic** (Claude) — https://console.anthropic.com/
- **Groq** (Free tier available) — https://console.groq.com/
- **Mistral** — https://console.mistral.ai/
- **Google Gemini** — https://aistudio.google.com/
- **OpenAI-compatible** (LM Studio, Ollama, etc.) — Provide base URL

Example for OpenAI:
```
LLM Provider: openai
API Key: sk-...
Model: gpt-4o
```

Example for Groq (free):
```
LLM Provider: groq
API Key: gsk-...
Model: mixtral-8x7b-32768
```

#### C. Campaign Details
```
Campaign Name: Tech SaaS Leads
Product Description: Cloud cost optimization platform for DevOps teams
Campaign Objective: Generate qualified leads for VP of Engineering at Series B startups
Booking Link: https://calendly.com/your-link
Seed LinkedIn URLs (optional): (Leave empty or paste profile URLs)
```

### Step 3: Verify Setup

Once onboarding completes, the daemon starts automatically and:

1. **Verifies credentials** — Tests LinkedIn login
2. **Initializes database** — Sets up SQLite with CRM schema
3. **Downloads ML model** — Fetches FastEmbed for embeddings
4. **Starts automation** — Begins discovering and qualifying leads

**Check logs for success:**
```
✓ LinkedInProfile created successfully
✓ Campaign initialized
✓ Database ready
✓ Starting lead discovery...
```

### Step 4: Watch it Work!

Open **http://localhost:6080/vnc.html** and you'll see:

- Firefox browser logging into LinkedIn
- Performing search queries
- Scraping profile information
- Creating LinkedIn connections
- Sending personalized messages

---

## 🔧 Configuration & Customization

### Rate Limits

Edit via Django Admin (/admin/linkedin/linkedinprofile/):

```
Connect Daily Limit: 20 (connections per day)
Connect Weekly Limit: 100 (connections per week)
Follow-up Daily Limit: 30 (messages per day)
```

**Safety tip:** Start low, increase gradually:
- Week 1: 5 connects/day → observe LinkedIn response
- Week 2: 10 connects/day → if no warnings, increase
- Week 3+: 20 connects/day → sustained operation

### Campaign Adjustment

In Django Admin (/admin/linkedin/campaign/), you can:

- **Add seed profiles** — Point AI to specific target profiles
- **Update product docs** — Refine product description
- **Change objective** — Adjust target market criteria
- **Update booking link** — Change meeting scheduler URL

### LLM Model Switching

LLM settings live on the `SiteConfig` DB row, not environment variables — edit via Django Admin
(SiteConfig model, reachable by direct URL at `/admin/linkedin/siteconfig/1/change/`) and restart
the daemon. There are two independent model slots: `chat_ai_model` (higher-end, used only by the
follow-up messaging agent) and `task_ai_model` (cheaper/faster, used for qualification, search
keywords, and fact extraction) — each with its own provider/key/base.

```bash
# Or from a shell in the container:
docker exec -it <container-id> python manage.py shell -c "
from linkedin.models import SiteConfig
cfg = SiteConfig.load()
cfg.chat_ai_model = 'gpt-4o'
cfg.task_ai_model = 'gpt-4o-mini'
cfg.save()
"

# Restart daemon
docker exec -it <container-id> python manage.py rundaemon
```

---

## 🔍 Monitoring & Troubleshooting

### Check Container Status

```bash
# List running containers
docker ps

# View logs
docker logs -f <container-id>

# Specific component logs
docker logs -f <container-id> 2>&1 | grep -i linkedin
docker logs -f <container-id> 2>&1 | grep -i error
```

### Database Inspection

```bash
# Enter container
docker exec -it <container-id> bash

# Access Django shell
python manage.py shell

# Check leads
from crm.models import Lead
Lead.objects.count()  # Total profiles discovered

# Check deals
from crm.models import Deal
Deal.objects.filter(state='READY_TO_CONNECT').count()  # Ready to contact

# View recent conversations
from chat.models import ChatMessage
ChatMessage.objects.order_by('-created_at')[:10]
```

### Common Issues

| Issue | Solution |
|-------|----------|
| **"LinkedIn login failed"** | Verify credentials, check if 2FA enabled on LinkedIn account. LinkedIn may require additional verification — try via VNC. |
| **"LLM API error"** | Verify API key, check quota/billing, ensure correct provider selected |
| **"No leads discovered"** | Campaign objective too specific. Try broader target market. Check LLM logs for qualification issues. |
| **Docker out of disk space** | Prune old containers/images: `docker system prune -a` |
| **VNC connection refused** | Check ports: `docker port <container-id>` should show 5900 and 6080 |
| **Slow performance** | Increase Docker memory: `--memory 4g` in docker run command |

### Performance Optimization

For **smoother operation**, allocate more resources:

```bash
docker run --pull always -it \
  --memory 4g \
  --cpus 2 \
  -p 5900:5900 \
  -p 6080:6080 \
  -v ~/.openoutreach/data:/app/data \
  ghcr.io/eracle/openoutreach:latest
```

- `--memory 4g` — 4GB RAM
- `--cpus 2` — 2 CPU cores

---

## 📊 Understanding the Workflow

### Lead Pipeline:

```
1. DISCOVERY
   ├─ AI generates search keywords based on campaign
   ├─ Automated browser searches LinkedIn
   └─ Profiles scraped → stored in database

2. ENRICHMENT
   ├─ Profile embeddings (384-dim vectors) computed
   ├─ Historical data used to warm-start ML model
   └─ Stored for qualification

3. QUALIFICATION
   ├─ Bayesian ML model (Gaussian Process) selects candidates
   ├─ LLM reviews each candidate (Yes/No decision)
   ├─ Model learns from every decision
   └─ Profiles promoted through: QUALIFIED → READY_TO_CONNECT

4. CONNECTION
   ├─ LinkedIn connection request sent
   ├─ Tracked, awaits acceptance
   └─ State: PENDING

5. ACCEPTANCE
   ├─ If accepted: State → CONNECTED
   ├─ AI-powered follow-up agent triggered
   └─ Sends personalized, contextual message

6. FOLLOW-UP
   ├─ Multi-turn conversation with prospect
   ├─ AI decides: Send message / Wait / Mark complete
   └─ Tracks engagement, meeting interest

7. CONVERSION
   └─ Deal marked COMPLETED with outcome (converted/interested/wrong_fit/etc.)
```

### Key Metrics to Monitor:

- **Discovery rate** — Profiles found per day
- **Qualification rate** — % qualified / discovered
- **Connection acceptance rate** — % who accept requests
- **Response rate** — % who reply to messages
- **Conversion rate** — % who book meetings

All visible in Django Admin (/admin/) and via VNC.

---

## 🛑 Stopping & Restarting

### Stop the Daemon (Pause Automation)

```bash
docker stop <container-id>
```

Data persists in `~/.openoutreach/data` — all profiles, messages, database stay.

### Restart (Resume Automation)

```bash
docker run --pull always -it \
  -p 5900:5900 \
  -p 6080:6080 \
  -v ~/.openoutreach/data:/app/data \
  ghcr.io/eracle/openoutreach:latest
```

The daemon resumes exactly where it left off.

### Full Reset (Clear Everything)

```bash
# Remove data volume
docker volume rm openoutreach_db

# Or delete directory
rm -rf ~/.openoutreach/data

# Next run will re-prompt onboarding
```

---

## 🚀 Advanced: LinkedIn Account Best Practices

### Safety & Compliance:

1. **Use a dedicated LinkedIn account** — Don't use your personal account
   - Create a business profile for outreach
   - LinkedIn may flag automation on personal accounts

2. **Warm up the account** — Before launching full campaigns:
   - Day 1-2: Make 5 manual connections
   - Day 3-5: Increase to 10 connections/day
   - Week 2+: Ramp to 20+ connections/day with daemon

3. **Monitor LinkedIn responses:**
   - Check browser VNC weekly for LinkedIn warnings
   - If you see "unusual activity" notices → reduce rate limits
   - Common limits: 20-30 connections/day, 50+ per week for aged accounts

4. **Avoid patterns LinkedIn detects:**
   - ✅ Variable timing between actions (daemon does this)
   - ✅ Human-like browser behavior (Playwright + stealth)
   - ❌ Sending messages to cold connects immediately
   - ❌ 100+ connections in first day
   - ❌ Generic templates without personalization

### Credentials:

Store LinkedIn credentials securely:
- ✅ Use a password manager (1Password, Bitwarden)
- ✅ Save in `.env` file (never commit to git)
- ❌ No plaintext in Docker commands

If using `.env` file:
```bash
# ~/.openoutreach/.env
LINKEDIN_EMAIL=your-email@example.com
LINKEDIN_PASSWORD=your-secure-password
```

Load it:
```bash
docker run --pull always -it \
  --env-file ~/.openoutreach/.env \
  -v ~/.openoutreach/data:/app/data \
  ghcr.io/eracle/openoutreach:latest
```

---

## 📝 Logs & Diagnostics

### View Logs in Real-time

```bash
docker logs -f <container-id>
```

Look for:
- `✓ LinkedIn authenticated` — Login successful
- `Discovered 12 profiles` — Search results retrieved
- `Qualified: John Doe (VP Engineering)` — LLM approved candidate
- `Connection sent to: jane@company.com` — Contact attempt
- `ERROR` — Any failures

### Save Logs for Analysis

```bash
# Export all logs
docker logs <container-id> > openoutreach-logs.txt

# Filter specific events
docker logs <container-id> 2>&1 | grep "CONNECTED\|FAILED\|ERROR"
```

### Enable Debug Mode

In Docker Compose (if building from source):

```yaml
# local.yml
services:
  app:
    environment:
      - DEBUG=true
      - LOG_LEVEL=DEBUG
```

Or via environment:
```bash
docker run -e DEBUG=true ... ghcr.io/eracle/openoutreach:latest
```

---

## 🎓 Example Campaign: Tech SaaS Lead Generation

### Scenario:
You sell **cloud cost optimization** for DevOps teams

### Campaign Setup:

```
Name: "Cloud Cost Optimization - Mid-Market SaaS"

Product Description:
"We help DevOps/Platform Engineering teams reduce AWS/GCP/Azure spending by 30-40% 
through intelligent container rightsizing and anomaly detection. Integrates with 
existing CI/CD pipelines."

Campaign Objective:
"VP/Director of DevOps/Platform Engineering at Series B-C funded SaaS companies 
(50-500 employees), in US/EU. Predict companies spending $100K+ monthly on cloud 
infrastructure."

Booking Link: https://calendly.com/your-link
```

### What Happens:

1. **AI discovers**: Searches like "VP DevOps Series B SaaS" → finds 100+ candidates
2. **ML learns**: From your feedback on 20 profiles, learns patterns:
   - ✓ Title: VP Engineering, Director DevOps, Platform Lead
   - ✓ Company: FinTech, AdTech, SaaS platforms
   - ✗ Title: Sales, Marketing, HR
   - ✗ Company: Agencies, freelancers
3. **Qualifies leads**: LLM reviews each candidate:
   - Reads profile (industry, company size, seniority)
   - Compares against ideal customer
   - Predicts likelihood they need your solution
4. **Contacts qualified**: Sends →
   ```
   "Hi John,
   I noticed you're building infrastructure at [Company]. We helped similar teams 
   at [similar company] reduce cloud spend by 35% without sacrificing performance. 
   Would you be open to a quick conversation about what's working for them?
   [Booking Link]"
   ```
5. **Follows up**: AI manages multi-turn conversation:
   - If ignored → Follow up after 3 days
   - If interested → Ask about priorities
   - If wants to talk → Close the loop

---

## 📞 Support & Community

- **Issues**: https://github.com/eracle/OpenOutreach/issues
- **Discussions**: https://github.com/eracle/OpenOutreach/discussions
- **Cloud Hosting**: https://openoutreach.app (zero-ops alternative)

---

## ✅ Checklist: You're Ready When...

- [ ] Docker installed and running
- [ ] Container starts without errors
- [ ] VNC accessible at http://localhost:6080/vnc.html
- [ ] Onboarding wizard completes (LinkedIn + LLM credentials saved)
- [ ] Django Admin accessible at http://localhost:8000/admin/
- [ ] First batch of profiles discovered (visible in CRM)
- [ ] Browser automation visible in VNC viewer
- [ ] No LinkedIn warnings/blocks on account

**Congratulations!** You now have a fully functional autonomous LinkedIn lead generation system. 🎉

---

## 🔗 Quick Reference

| Task | Command |
|------|---------|
| Start | `docker run --pull always -it -p 5900:5900 -p 6080:6080 -v ~/.openoutreach/data:/app/data ghcr.io/eracle/openoutreach:latest` |
| Stop | `docker stop <container-id>` |
| Logs | `docker logs -f <container-id>` |
| Access VNC | http://localhost:6080/vnc.html |
| Admin | http://localhost:8000/admin/ (after `createsuperuser`) |
| Enter Container | `docker exec -it <container-id> bash` |
| Database Shell | `python manage.py shell` |
| Reset Data | `rm -rf ~/.openoutreach/data` |
