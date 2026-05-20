# Email Server (MCP)

A **Model Context Protocol (MCP) server** that connects to Gmail via IMAP to analyze and monitor your inbox.

## What It Does

Provides **two comprehensive tools** for email management:

1. **Email Analysis** - Analyzes unread and recent emails with sender grouping and timelines
2. **Email Monitoring** - Tracks email statistics, patterns, and inbox health

## How It Works

```
┌─────────────────┐
│  MCP Client     │  (Claude Desktop, etc.)
│  calls tools    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  email_server   │
│  .py            │
├─────────────────┤
│ GmailClient     │──► Connects to Gmail IMAP
│                 │──► Fetches emails (unread/recent)
│                 │──► Parses email content
│                 │
│ EmailProcessor  │──► Groups by sender
│                 │──► Creates timelines
│                 │──► Generates statistics
└─────────────────┘
         │
         ▼
┌─────────────────┐
│  Gmail Inbox    │
│  (IMAP)         │
└─────────────────┘
```

## Setup

### 1. Enable Gmail IMAP

1. Go to Gmail Settings → **See all settings** → **Forwarding and POP/IMAP**
2. Enable **IMAP access**
3. Save changes

### 2. Create App Password

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (if not already enabled)
3. Go to **App passwords**
4. Generate password for "Mail" on "Other device"
5. Copy the 16-character password

### 3. Configure Environment

Create `.env` file in the same directory:

```env
EMAIL_ADDRESS=your.email@gmail.com
APP_PASSWORD=your_16_char_app_password
```

### 4. Install Dependencies

```bash
pip install mcp pydantic python-dotenv
```

### 5. Run the Server

```bash
python email_server.py
```

Or configure in MCP client (e.g., Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "email": {
      "command": "python",
      "args": ["/path/to/email_server.py"]
    }
  }
}
```

## Available Tools

### 1. `comprehensive_email_analysis`

Analyzes your inbox with detailed breakdowns.

**Parameters:**
- `unread_count` (int): Number of unread emails to analyze (default: 50, max: 100)
- `recent_count` (int): Number of recent emails to include (default: 100, max: 200)
- `include_timeline` (bool): Include date-based timeline (default: true)
- `include_sender_groups` (bool): Group emails by sender (default: true)
- `include_metadata` (bool): Include detailed metadata (default: true)

**Returns:**
```json
{
  "summary": {
    "total_unread": 45,
    "total_recent_analyzed": 100,
    "unread_with_attachments": 12,
    "recent_emails_today": 8
  },
  "unread_emails": { ... },
  "sender_analysis": { ... },
  "timeline_analysis": { ... },
  "insights": {
    "unread_backlog": "Medium",
    "attachment_ratio": 26.7,
    "recommendations": [...]
  }
}
```

### 2. `comprehensive_email_monitoring`

Monitors email patterns and inbox health.

**Parameters:**
- `count` (int): Number of emails to analyze (default: 200, max: 500)
- `keywords` (array): Keywords to track (e.g., `["meeting", "urgent", "deadline"]`)
- `top_senders_limit` (int): Top senders to show (default: 15, max: 50)
- `include_productivity_score` (bool): Calculate productivity score (default: true)
- `include_detailed_recommendations` (bool): Get actionable insights (default: true)

**Returns:**
```json
{
  "summary": {
    "total_emails": 2847,
    "unread_emails": 45,
    "read_percentage": 98.4,
    "unique_senders": 127
  },
  "email_type_analysis": {
    "marketing": 32,
    "work": 45,
    "personal": 18,
    "notification": 15
  },
  "productivity_analysis": {
    "score": 72.5,
    "rating": "Good"
  },
  "recommendations": [...]
}
```

## Key Features

✅ **Email Classification** - Automatically categorizes emails (work, marketing, personal, etc.)
✅ **Sender Analysis** - Identifies frequent senders and concentration patterns
✅ **Timeline Tracking** - Daily email distribution and patterns
✅ **Keyword Monitoring** - Track specific keywords across emails
✅ **Productivity Scoring** - Rates inbox health based on email types
✅ **Smart Recommendations** - Actionable insights for inbox management
✅ **Date Intelligence** - Identifies recent emails, time periods, weekends

## Architecture

### Core Components

1. **GmailClient** - IMAP connection handler
   - `connect()` - Establishes Gmail IMAP connection
   - `fetch_unread_emails()` - Gets unread messages
   - `fetch_recent_emails()` - Gets recent messages
   - `fetch_email_statistics()` - Gathers inbox metrics
   - Email classification, domain extraction, keyword search

2. **EmailProcessor** - Data organization
   - `prepare_emails_for_analysis()` - Formats email data
   - `group_emails_by_sender()` - Groups by sender
   - `create_date_timeline()` - Builds timeline

3. **Helper Functions**
   - `_generate_summary_recommendations()` - Creates actionable advice
   - `_calculate_productivity_score()` - Scores inbox health
   - `_generate_actionable_insights()` - Priority-based insights

## Security Notes

⚠️ **Current Issue**: Lines 35-36 in `email_server.py` have hardcoded credentials as fallbacks
✅ **Fix**: Ensure `.env` file exists with your credentials
✅ **Best Practice**: Never commit `.env` to version control (add to `.gitignore`)

## Troubleshooting

**"Gmail connection failed"**
- Check IMAP is enabled in Gmail settings
- Verify app password is correct (16 chars, no spaces)
- Ensure 2FA is enabled on Google account

**"No emails found"**
- Check that you have emails in your inbox
- Try reducing `count` parameter
- Check server logs for IMAP errors

**"Error: imaplib module not found"**
- imaplib is built into Python - check Python version (3.7+)

## Example Usage (via MCP Client)

```
User: "Analyze my last 50 emails"
→ Calls comprehensive_email_analysis with default parameters
→ Returns summary, sender groups, timeline, insights

User: "Monitor my inbox for emails about 'sports' and 'baseball'"
→ Calls comprehensive_email_monitoring with keywords: ["sports", "baseball"]
→ Returns statistics with keyword tracking results
```

## API Reference

### Tool: comprehensive_email_analysis

```python
{
  "unread_count": 50,        # 1-100
  "recent_count": 100,       # 20-200
  "include_timeline": true,
  "include_sender_groups": true,
  "include_metadata": true
}
```

### Tool: comprehensive_email_monitoring

```python
{
  "count": 200,                           # 50-500
  "keywords": ["urgent", "meeting"],      # Optional array
  "top_senders_limit": 15,                # 5-50
  "include_productivity_score": true,
  "include_detailed_recommendations": true
}
```

## Data Models

### EmailMessage
```python
{
  "uid": str,              # Unique email ID
  "subject": str,          # Email subject
  "sender": str,           # Sender email/name
  "date": str,             # Formatted date
  "body": str,             # Email body (truncated to 500 chars)
  "has_attachments": bool,
  "is_read": bool,
  "date_info": {
    "is_recent": bool,     # < 24 hours old
    "is_today": bool,      # Received today
    "age_hours": float,
    "time_period": str     # morning/afternoon/evening/night
  }
}
```

### EmailStats
```python
{
  "total_emails": int,
  "unread_emails": int,
  "read_emails": int,
  "emails_with_attachments": int,
  "sender_distribution": dict,
  "domain_distribution": dict,
  "date_distribution": dict,
  "time_distribution": dict,
  "email_types": dict,
  "keyword_matches": dict,
  "recommendations": list
}
```

## File Structure

```
email_server.py          # Main MCP server
.env                     # Email credentials (not committed)
README_EMAIL_SERVER.md   # This file
```

## Contributing

To extend this server:

1. Add new methods to `GmailClient` for fetching different email types
2. Add new processing methods to `EmailProcessor`
3. Register new tools in `@server.list_tools()`
4. Implement tool handlers in `@server.call_tool()`

## License

This is part of a homework/project assignment (ece153a-main).

---

**Built with**: Python 3.7+ | MCP Protocol | Gmail IMAP
**Dependencies**: `mcp`, `pydantic`, `python-dotenv`
**File**: `email_server.py`
