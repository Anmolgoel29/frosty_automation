# LinkedIn Outreach Setup - Best Practices & Configuration Guide

## 🔗 LinkedIn Account Preparation

### 1. Account Creation Checklist

**Create a dedicated business/outreach account:**

```
✅ Email: business.outreach@domain.com (separate from personal)
✅ First Name: Your Company / Bot Name (optional)
✅ Last Name: Sales / Outreach
✅ Profile Picture: Company logo or professional photo
✅ Headline: "Building B2B connections | [Your Company]"
✅ Bio: Brief company description (keep professional)
✅ Website: Link to your company
```

**Why separate account?**
- LinkedIn monitors/flags automation in old personal accounts
- New accounts have less reputation → LinkedIn watches more carefully initially
- Easier to adjust rate limits without affecting personal profile
- Separates your personal network from automated campaigns

### 2. Account Warm-up (Critical!)

**LinkedIn's anti-bot system** learns from your behavior. Don't immediately:
```
❌ Send 50 connection requests on Day 1
❌ Send identical messages to everyone
❌ Connect during off-hours (3 AM)
❌ Use a VPN from different country each day
```

**Recommended warm-up timeline:**

| Period | Connections/Day | Messages/Day | Notes |
|--------|-----------------|--------------|-------|
| **Week 1** | 3-5 | 0 | Manual connects only, read some profiles |
| **Week 2** | 8-10 | 2-3 | Start daemon at 50% rate limit |
| **Week 3** | 15-20 | 5-10 | Increase if no warnings |
| **Week 4+** | 20-30 | 20-30 | Peak operation (monitor weekly) |

### 3. Enable 2-Factor Authentication (Recommended)

```
LinkedIn Settings → Account → Sign in & security → Two-step verification
```

Supported methods:
- SMS (recommended for automated flows)
- Mobile app
- Security key

**For OpenOutreach:**
- If using SMS 2FA, you'll need to provide verification code on first login in VNC
- After initial verification, credentials are saved → no 2FA needed for restarts

### 4. Adjust Privacy Settings

```
LinkedIn Settings → Privacy → Who can see your connections
```

Set to: **Only you** (prevents profile crawling)

```
LinkedIn Settings → Privacy → Allow people to see your full profile
```

Set to: **Your connections** (allows automation to see profiles it finds)

---

## 🚀 OpenOutreach LinkedIn Integration

### First Setup in Docker

When you start the container, onboarding prompts for:

```bash
LinkedIn Email: business.outreach@yourcompany.com
LinkedIn Password: [Secure password - OpenOutreach stores encrypted]
```

**Sample VNC flow (first login):**

1. Container starts
2. Firefox opens LinkedIn login page
3. You see in VNC: Email/password fields
4. Daemon attempts login
5. If 2FA enabled:
   - SMS arrives with code
   - Daemon waits for code input
6. Successfully logs in → cookies saved
7. On future restarts, uses saved session (no login needed)

### LinkedIn Detection Avoidance

OpenOutreach uses **Playwright + anti-detection plugins:**

```python
# playwright-stealth plugin (built-in)
# Hides automation indicators from LinkedIn JS detection:
- navigator.webdriver = undefined
- Hides headless/chrome flags
- Mimics real browser memory patterns
```

**How connections are sent:**

```
1. Browser navigates to profile URL
2. Waits 2-5 seconds (random, human-like delay)
3. Clicks "Connect" button (visual automation)
4. Adds optional note: "Hi [Name], [personalized message]"
5. Waits 3-30 seconds before next action
6. Moves to next profile
```

**How messages are sent:**

```
1. For accepted connections → Check for message thread
2. Generate personalized message (LLM + product context)
3. Click message box → Type message
4. Wait 5-15 seconds (simulate typing)
5. Click Send
6. Record message, track responses
```

---

## 📊 Campaign Configuration

### Example: B2B SaaS Financial Services

```
Campaign Name:
  "RegTech Risk Monitoring - Mid-Market Banking"

Product Description:
  "We provide AI-powered regulatory compliance monitoring for financial 
   institutions. Our platform automates KYC/AML checks, detects sanctions 
   list violations in real-time, and produces audit reports. Integrates with 
   existing core banking systems."

Campaign Objective:
  "Chief Risk Officer / AVP Compliance at mid-market banks (50-500 employees) 
   in US/EU, specifically those using core systems like Temenos, Fiserv, 
   or internal systems. Looking for prospects in regulatory-heavy verticals: 
   payments, lending, wealth management."

Booking Link:
  "https://calendly.com/your-company/compliance-demo"

Seed LinkedIn URLs (optional):
  https://www.linkedin.com/in/jane-risk-officer/
  https://www.linkedin.com/in/john-compliance-lead/
```

**Why specificity matters:**

- **Product description** → LLM uses to qualify prospects
  - Specific features (KYC/AML, sanctions checks)
  - Target personas (CRO, AVP Compliance, VP Risk)
  - Integration points (core banking systems)
  - Vertical focus (payments, lending)

- **Campaign objective** → AI generates search keywords
  - "Chief Risk Officer" vs vague "compliance"
  - "Mid-market banks" vs all companies
  - "US/EU" → filters geography
  - "Core banking systems" → narrows to tech-savvy audience

- **Seed URLs** → ML model learns from your examples
  - AI analyzes these 2-3 profiles
  - Generates embeddings (384-dim vectors)
  - Uses pattern to find similar profiles

### Example: Developer Tool / DevOps

```
Campaign Name:
  "Infrastructure Monitoring - Fast-Growing SaaS"

Product Description:
  "Real-time infrastructure monitoring platform for micro-services architectures. 
   Detects anomalies before customers impact, provides auto-remediation, integrates 
   with Kubernetes and container registries. Better than Datadog for cost and 
   ease of setup."

Campaign Objective:
  "Director/VP of DevOps/Infrastructure at Series B-D SaaS companies 
   (100-2000 employees), US/EU, companies using AWS/GCP/Kubernetes. 
   Ideal if already paying for APM tools and want to consolidate."

Booking Link:
  "https://calendly.com/your-company/infra-demo"
```

---

## ⚙️ Rate Limit Configuration

### Safe Starting Points by Account Age

| Account Age | Daily Limit | Weekly Limit | Follow-up Daily | Notes |
|-------------|------------|-------------|-----------------|-------|
| **< 1 week** | 3-5 | 10-20 | 0 | Manual only, just setting up |
| **1-2 weeks** | 5-8 | 30-50 | 2-3 | Start daemon at 25% capacity |
| **2-4 weeks** | 10-15 | 50-70 | 5-10 | If no warnings, gradually increase |
| **1-3 months** | 15-25 | 80-120 | 15-20 | Monitor weekly for warnings |
| **3+ months** | 25-40 | 150-200 | 20-30 | Aged account: less restrictive |

### How to Adjust in OpenOutreach

**Via Django Admin:**

```
1. Open http://localhost:8000/admin/
2. LinkedInProfile section
3. Edit your profile:
   - connect_daily_limit: 20
   - connect_weekly_limit: 100
   - follow_up_daily_limit: 30
4. Save
```

**Via Django Shell:**

```bash
docker exec -it <container-id> python manage.py shell

from linkedin.models import LinkedInProfile
profile = LinkedInProfile.objects.first()
profile.connect_daily_limit = 20
profile.connect_weekly_limit = 100
profile.follow_up_daily_limit = 30
profile.save()
```

### LinkedIn Rate Limit Warnings

**What you'll see in VNC if pushing too hard:**

```
⚠️ "LinkedIn has temporarily blocked some of your actions"
→ Reduce rate limits by 50%
→ Wait 3-5 days
→ Gradually increase again

⚠️ "Unusual activity detected"  
→ Stop automation immediately
→ Wait 7 days
→ Resume at 5 connects/day

🚫 Account disabled
→ LinkedIn thinks you're a bot
→ May need account recovery
→ Use lower limits in future
```

**Prevention:**

```
✅ Start low: 5-10 connections/day Week 1
✅ Monitor: Check LinkedIn every 2-3 days
✅ Gradual: Increase 20% every 2 weeks
✅ Varied timing: Daemon uses human-like schedules
✅ Breaks: Automation includes 10-20 min breaks every hour
```

---

## 💬 Message Personalization

### LLM Prompt System

OpenOutreach uses **Jinja2 templates** for message generation:

**Default template:**
```
Hi {{ first_name }},

I noticed you're {{ profile_summary }} at {{ company_name }}.
We help {{ ideal_customer_description }} {{ solve_problem }}.

{{ company_name }} reminds me of {{ similar_company }} - 
they saw {{ metric }} improvement from our solution.

Would you be open to a quick conversation?

[{{ booking_link }}]
```

**LLM fills in:**
- `first_name` → "John"
- `company_name` → "Stripe"
- `profile_summary` → "VP of Engineering"
- `ideal_customer_description` → "fast-growing payments platforms"
- `similar_company` → "Wise" (if in embeddings)
- `metric` → "40% cost reduction"

### How to Customize Messages

In Django Admin:

```
1. LinkedIn → Campaign
2. Edit campaign
3. Customize:
   - product_docs: More specific product info
   - campaign_objective: Better targeting criteria
4. LLM's next messages auto-adjust
```

### Follow-up Sequences

Configured in [docs/templating.md](../docs/templating.md):

**Example automated sequence:**

```
Day 0:  Connection request sent → "Hi [Name], I think we could help..."
Day 3:  If not accepted → Resend or wait
Day 5:  If accepted, No message → "Following up on my earlier note..."
Day 7:  If ignored → Final attempt: "Just wanted to check in one more time..."
Day 10: Mark as unresponsive (stop trying)
```

---

## 🔐 Security & Data Privacy

### Credentials Storage

**OpenOutreach stores:**
- ✅ LinkedIn credentials (encrypted in database)
- ✅ LLM API keys (encrypted)
- ✅ Profile data (embeddings, interactions)
- ✅ Campaign configuration
- ✅ Message history

**OpenOutreach does NOT:**
- ❌ Send data to external services (except LLM provider for classification)
- ❌ Store data in cloud (stays on your Docker volume)
- ❌ Share with third parties
- ❌ Use for training AI models

### GDPR Compliance

If running in EU/UK/Canada:

```
LinkedIn Settings → Legal
✓ Newsletter subscription automatically disabled
✓ GDPR-protected location detected on first run
✓ Legal notice acceptance recorded per account
```

### Data Locations

All data stays on your `~/.openoutreach/data`:

```
~/.openoutreach/data/
├── db.sqlite3              # Main database (leads, deals, configs)
├── logs/                   # Application logs
└── playwright/
    └── cookies             # LinkedIn saved session (encrypted)
```

**Backup before campaigns:**

```bash
cp -r ~/.openoutreach/data ~/.backup-openoutreach-$(date +%Y%m%d)
```

---

## 🛡️ Legal & LinkedIn ToS Compliance

### Key Points:

**LinkedIn's terms allow:**
- ✅ Programmatic connection requests (with LI API)
- ✅ Browser automation (Playwright is legitimate)
- ✅ Message sending (through official UI)
- ✅ Profile scraping (public profile data)

**LinkedIn's terms prohibit:**
- ❌ Account takeover / credential sharing
- ❌ Scraping phone numbers / emails (from LinkedIn)
- ❌ Mass spam (same message to 1000+ people)
- ❌ Using fake/bot accounts
- ❌ Harvesting contact info for sales

### How OpenOutreach Complies:

```
✅ Uses YOUR actual LinkedIn account (not fake)
✅ Respects rate limits (doesn't spam)
✅ Personalizes every message (LLM + product context)
✅ Targets specific roles/companies (not random spray)
✅ Doesn't scrape email addresses (uses messaging only)
✅ Provides opt-out via "not interested" response
✅ Legal acceptance required at onboarding
✅ Respects GDPR restrictions per jurisdiction
```

### Acceptable Use Policy

By using OpenOutreach you agree:

1. **Account ownership** — You own the LinkedIn account
2. **Accuracy** — Campaign descriptions are honest
3. **Respect** — Recipients can mute/block without harm
4. **Compliance** — You follow LinkedIn + local laws
5. **Rate limiting** — You respect LinkedIn's anti-spam measures
6. **No harassment** — You won't message the same person excessively

---

## 📈 Campaign Results & Analytics

### Metrics Dashboard

In Django Admin (/admin/):

```
Campaign Statistics:
├─ Total Leads Discovered: 1,234
├─ Qualified (ML approved): 456
├─ Ready to Connect: 234
├─ Connection Requests Sent: 200
├─ Accepted Connections: 87 (43.5%)
├─ Messages Sent: 65
├─ Responses: 18 (27.7%)
├─ Meetings Booked: 3
└─ Conversion Rate: 1.5% (3 meetings from 200 prospects)
```

### Key Performance Indicators (KPIs)

| Metric | Healthy Range | Red Flag |
|--------|---------------|----------|
| **Qualification Rate** | 30-50% | < 10% = too strict objectibe |
| **Acceptance Rate** | 30-50% | < 20% = maybe connections too aggressive |
| **Response Rate** | 20-40% | < 10% = bad messaging |
| **Meeting Rate** | 5-15% | < 2% = wrong audience |

### Optimizing Low Performance

**If Acceptance Rate is low (< 20%):**
```
1. Reduce connection rate (space out more)
2. Check messaging: Add more context
3. Review seed profiles: Are they really your ideal customer?
4. Adjust objective: May be targeting wrong roles
```

**If Response Rate is low (< 10%):**
```
1. Improve message personalization (add company-specific context)
2. Reduce follow-up delays (respond faster)
3. Better lead qualification (fewer wrong-fit people)
4. Update product description (clearer value prop)
```

**If Booking Rate is low (< 5% of responses):**
```
1. More aggressive follow-up (ask directly for meeting)
2. Shorter booking link text
3. Different booking time option (async vs sync)
4. Include company wins (social proof in messages)
```

---

## 🚀 Running Multiple Campaigns

### Architecture:

One OpenOutreach instance can run **multiple campaigns**:

```
Campaign 1: "RegTech - Risk Management"
└─ Targets: CROs, Compliance Officers
└─ Message: Regulatory compliance focus

Campaign 2: "FinTech - Payment Processing"
└─ Targets: VP Product, Head of Engineering
└─ Message: Payment infrastructure focus
```

### Setup Multiple Campaigns:

**In Django Admin:**

```
1. LinkedIn → Campaigns → Add Campaign
   - Name: "RegTech - Risk Management"
   - Product docs: [compliance-specific]
   - Campaign objective: [compliance-specific]
   - Booking link: [shared or different]

2. LinkedIn → Campaigns → Add Campaign #2
   - Name: "FinTech - Payment Processing"
   - Product docs: [payments-specific]
   - Campaign objective: [payments-specific]

3. Save both campaigns
```

**Daemon automatically:**
- Distributes connections between campaigns
- Tracks leads per campaign
- Learns ML model per campaign
- Manages separate conversation threads

### Rate Limit Distribution

With `connect_daily_limit = 20` and 2 campaigns:

```
Campaign 1: ~10 connections/day
Campaign 2: ~10 connections/day
Total: 20 connections/day
```

Distribution is proportional to campaign priority/traffic.

---

## 🔧 Troubleshooting LinkedIn Issues

### "LinkedIn Login Failed"

**Cause:** Credentials incorrect or account locked

**Solution:**
```bash
1. Verify email/password manually on linkedin.com ✓
2. If using 2FA:
   - Access VNC (http://localhost:6080)
   - Wait for SMS code prompt
   - Enter code in browser
3. If account locked:
   - LinkedIn recovery email
   - Verify identity
   - Try again in 24 hours
```

### "LinkedIn Temporarily Blocked Our Actions"

**Cause:** Rate limit exceeded (more likely with new accounts)

**Solution:**
```bash
1. Stop automation immediately
   docker stop openoutreach
2. Wait 48 hours
3. Reduce rate limits by 50%
   - Daily: 20 → 10
   - Weekly: 100 → 50
4. Restart with lower limits
```

### "No Profiles Found / Too Few Connections"

**Cause:** Campaign objective too narrow or search keywords weak

**Solution:**
```
1. Broaden campaign objective
   ❌ "CEO at YC-funded AI startups worth $1B+"
   ✅ "VP/Director at AI startups, Series A-B"

2. Add seed profiles
   - Go to Django Admin → Campaign
   - Add LinkedIn profile URLs of ideal customers
   - ML model learns from seeds

3. Check LLM logs
   - docker logs -f <container> | grep -i qualify
   - Might be rejecting too many candidates
```

### "Messages Not Sending"

**Cause:** LinkedIn connection not properly accepted or message thread not found

**Solution:**
```
1. Check acceptance in VNC: View connections
2. Ensure message thread exists before sending
3. Check follow-up rate limits (might be exhausted)
4. Increase follow_up time (wait longer before sending)
```

---

## 📚 Additional Resources

- [Configuration Guide](./configuration.md) — Deep dive into all settings
- [Templating Guide](./templating.md) — Custom message templates
- [GDPR Roadmap](./GDPR_ROADMAP.md) — Compliance & data handling
- [Architecture](./architecture.md) — Technical deep dive
- GitHub Issues — Community support: https://github.com/eracle/OpenOutreach/issues

---

## ✅ LinkedIn Setup Checklist

- [ ] Created dedicated outreach LinkedIn account
- [ ] Added profile picture & bio
- [ ] Enabled 2FA (SMS recommended)
- [ ] Connected 5-10 people manually (warm-up)
- [ ] Waited 1 week after account creation
- [ ] Docker OpenOutreach running
- [ ] Entered LinkedIn credentials in onboarding
- [ ] Configured campaign (product + objective)
- [ ] Set realistic rate limits (start low: 5-10/day)
- [ ] First batch of profiles discovered
- [ ] Monitor VNC for LinkedIn warnings
- [ ] Request connection visible in Django Admin
- [ ] Responses tracked in message threads
- [ ] Metrics showing in dashboard

**Once all checked** → You're ready for full campaign operation! 🚀
