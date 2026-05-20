# Fantasy Baseball Agent

An intelligent fantasy baseball assistant powered by Claude AI and the Model Context Protocol (MCP). This agent provides data-driven insights, player analysis, and strategic recommendations by integrating real-time MLB statistics with AI-powered analysis.

## Overview

The Fantasy Baseball Agent is an MCP-based system that combines:
- **Claude AI** for natural language understanding and strategic analysis
- **MLB Stats API** for real-time player statistics and data
- **Local league management** for personalized team recommendations
- **Agentic workflow** for multi-step reasoning and tool orchestration

## Features

### Player Analysis
- 🔍 **Player Search** - Find players by name across MLB
- 📊 **Comprehensive Stats** - Access detailed batting, pitching, and advanced metrics
- 🏆 **League Leaders** - View top performers in any statistical category
- 📈 **Multi-Player Comparisons** - Batch analyze multiple players simultaneously

### Fantasy Management
- 🎯 **Daily Lineup Recommendations** - Get start/sit advice for your roster
- 🔄 **Trade Evaluation** - Analyze proposed trades with data-backed assessments
- 💎 **Waiver Wire Pickups** - Identify valuable free agents
- 📋 **League Configuration** - Manage your fantasy league teams and rosters

### Data Integration
- ✅ Real-time data from MLB's official Stats API
- ✅ Support for current and historical seasons
- ✅ Traditional and advanced analytics (OPS, wRC+, FIP, xFIP, etc.)
- ✅ Statcast metrics (Exit Velocity, Barrel%, Hard Hit%)

## Architecture

```
┌─────────────┐
│    User     │
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────┐
│  fantasy_test_client.py         │
│  (CLI or REST API Interface)    │
│  - Anthropic Claude API Client  │
│  - Message History Management   │
│  - Agentic Loop Orchestration   │
└────────────┬────────────────────┘
             │ MCP Protocol (stdio)
             ▼
┌─────────────────────────────────┐
│  fantasy_server.py              │
│  (MCP Server)                   │
│  - Tool Registration            │
│  - Request Routing              │
│  - Parameter Validation         │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  fantasy_tools.py               │
│  (Business Logic Layer)         │
│  - MLB API Integration          │
│  - Statistical Analysis         │
│  - Recommendation Engine        │
└────────────┬────────────────────┘
             │
             ├──────────────────────┐
             ▼                      ▼
┌──────────────────────┐  ┌────────────────────┐
│  fantasy_config.py   │  │  MLB Stats API     │
│  (Config Manager)    │  │  (External)        │
└──────────┬───────────┘  └────────────────────┘
           │
           ▼
┌──────────────────────┐
│ fantasy_league.json  │
│ (League Data Store)  │
└──────────────────────┘
```

## Installation

### Prerequisites

- Python 3.8 or higher
- pip package manager
- Anthropic API key

### Step 1: Install Dependencies

```bash
pip install -r requirements.txt
```

Required packages:
- `anthropic` - Claude AI SDK
- `mcp` - Model Context Protocol
- `requests` - HTTP client for MLB API
- `python-dotenv` - Environment variable management
- `fastapi` - REST API framework (for API mode)
- `uvicorn` - ASGI server (for API mode)
- `pydantic` - Data validation

If `requirements.txt` doesn't exist, install manually:
```bash
pip install anthropic mcp requests python-dotenv fastapi uvicorn pydantic anyio
```

### Step 2: Set Up Environment Variables

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your_api_key_here
USER_NAME=YourName
ANTHROPIC_MODEL=claude-sonnet-4-20250514
MAX_TOKENS=8000
```

**Required**:
- `ANTHROPIC_API_KEY` - Get from [Anthropic Console](https://console.anthropic.com/)

**Optional**:
- `USER_NAME` - Display name for CLI prompts (default: "")
- `ANTHROPIC_MODEL` - Claude model to use (default: "claude-sonnet-4-20250514")
- `MAX_TOKENS` - Max response length (default: 8000)

### Step 3: Configure Your League

Edit `fantasy_league.json` to add your fantasy league teams:

```json
{
  "league_name": "Your League Name",
  "teams": [
    {
      "name": "Your Team Name",
      "hitters": [
        "Player Name 1",
        "Player Name 2"
      ],
      "pitchers": [
        "Pitcher Name 1",
        "Pitcher Name 2"
      ]
    }
  ]
}
```

## Configuration

### League Configuration File

The `fantasy_league.json` file stores your fantasy league data:

**Structure**:
- `league_name` - Name of your fantasy league
- `teams` - Array of team objects
  - `name` - Team name
  - `hitters` - Array of hitter names
  - `pitchers` - Array of pitcher names

**Example**:
```json
{
  "league_name": "Champions League 2025",
  "teams": [
    {
      "name": "The Bombers",
      "hitters": [
        "Aaron Judge",
        "Mookie Betts",
        "Ronald Acuña Jr."
      ],
      "pitchers": [
        "Shohei Ohtani",
        "Gerrit Cole"
      ]
    }
  ]
}
```

### System Prompt Customization

The client includes a comprehensive system prompt that instructs Claude on how to analyze fantasy baseball questions. You can:

**View current prompt**:
```
/system
```

**Update prompt** (in CLI mode):
```
/system: Your custom prompt here
```

The default prompt emphasizes:
- Data-driven analysis with numerical statistics
- Markdown formatting for readability
- Multi-step tool orchestration
- Comprehensive statistical coverage

## Usage

### CLI Mode

Interactive command-line interface for natural conversations with the agent.

**Start the client**:
```bash
python fantasy_test_client.py fantasy_server.py
```

**Commands**:
- Type your questions naturally
- `/system` - View current system prompt
- `/system: <prompt>` - Update system prompt
- `exit` - Quit the application

**Output**:
- Responses display in terminal
- Conversation saved to `client_output.md`
- Server logs written to `fantasy_baseball_server.log`
