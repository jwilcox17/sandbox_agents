# Coding Tools MCP Server

An **MCP (Model Context Protocol) server** that provides comprehensive coding research and file management tools.

## What It Does

Combines **web research** and **file operations** into one MCP server:

**Web Research Tools:**
- Search Stack Overflow (via official API)
- Fetch GitHub README files
- Search Python packages on PyPI
- Find code usage examples
- Get comprehensive info (combines all sources)

**File Operations:**
- Read local files
- Write/append to files
- List directory contents

## Quick Start

### 1. Install Dependencies

```bash
# No external dependencies needed! Uses Python standard library (urllib)
python --version  # Requires Python 3.7+
```

### 2. Run the Server

```bash
cd agents/
python coding_server.py
```

Or configure in your MCP client (e.g., Claude Desktop):

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "python",
      "args": ["/path/to/agents/coding_server.py"]
    }
  }
}
```

### 3. Start Using Tools

The server will automatically start and expose 8 tools to your MCP client.

## Available Tools

### 🔍 Web Research Tools

#### 1. `search_stackoverflow`
Search Stack Overflow for coding questions and answers.

```json
{
  "query": "how to parse JSON in python"
}
```

**Returns:** Top 5 results with title, link, score, answer count, and tags.

---

#### 2. `get_github_readme`
Fetch README from a GitHub repository.

```json
{
  "repo": "requests/requests"
}
```
or
```json
{
  "repo": "requests"  // Searches and finds top result
}
```

**Returns:** README content (first 2000 chars), repo name, and README URL.
**Supports:** README.md, README.rst, master/main branches

---

#### 3. `search_pypi`
Get Python package information from PyPI.

```json
{
  "package": "requests"
}
```

**Returns:**
```
Name: requests
Version: 2.31.0
Summary: Python HTTP for Humans.
Author: Kenneth Reitz
License: Apache 2.0
Description: [package description]
```

---

#### 4. `find_usage_examples`
Find real-world code examples for a library/API.

```json
{
  "query": "requests library"
}
```

**Returns:** Up to 3 code examples extracted from Stack Overflow answers.

---

#### 5. `get_comprehensive_info`
Get all available information about a tool/library (combines PyPI, GitHub, examples, docs).

```json
{
  "query": "flask"
}
```

**Returns:**
- PyPI package info
- GitHub README
- Code usage examples
- Documentation links

---

### 📁 File Tools

#### 6. `read_file`
Read contents of a local file.

```json
{
  "file_path": "/path/to/file.py"
}
```

**Returns:** File content formatted as markdown.

---

#### 7. `write_file`
Write or append content to a file.

```json
{
  "file_path": "/path/to/output.txt",
  "content": "Hello, world!",
  "append": false  // true to append, false to overwrite
}
```

**Returns:** Success/error message.

---

#### 8. `list_directory`
List files and directories with metadata.

```json
{
  "directory": "/path/to/dir"  // defaults to current directory
}
```

**Returns:** Formatted markdown table with:
- File/directory names
- Type (File/Directory)
- Size in bytes
- Last modified timestamp

## Example Usage

### Research a New Library

**User:** "I want to learn about the FastAPI library"

**MCP Client calls:**
```
get_comprehensive_info(query="fastapi")
```

**Returns:**
- PyPI info (version, author, description)
- GitHub README
- Code examples from Stack Overflow
- Documentation links

---

### Find Solution to Error

**User:** "How do I fix 'ModuleNotFoundError' in Python?"

**MCP Client calls:**
```
search_stackoverflow(query="ModuleNotFoundError python fix")
```

**Returns:** Top 5 Stack Overflow questions with scores and links.

---

### Read and Analyze Code

**User:** "Read the contents of my main.py file"

**MCP Client calls:**
```
read_file(file_path="/path/to/main.py")
```

**Returns:** Formatted file content.

---

### Save Generated Code

**User:** "Save this code to utils.py"

**MCP Client calls:**
```
write_file(
  file_path="utils.py",
  content="def hello():\n    print('Hello!')",
  append=false
)
```

**Returns:** "Wrote file: utils.py"

## Architecture

```
┌──────────────────┐
│   MCP Client     │ (Claude Desktop, etc.)
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────┐
│   coding_server.py           │
│   (MCP Server)               │
├──────────────────────────────┤
│  Route tools to:             │
│  • WebResearchToolkit        │
│  • FileTools                 │
└────────┬─────────────────────┘
         │
         ▼
┌──────────────────────────────┐
│   coding_tools.py            │
│   (WebResearchToolkit)       │
├──────────────────────────────┤
│  • StackExchange API         │──► api.stackexchange.com
│  • GitHub raw files          │──► raw.githubusercontent.com
│  • PyPI JSON API             │──► pypi.org/pypi/{pkg}/json
│  • Code extraction           │
└──────────────────────────────┘
```

## Key Features

✅ **Official APIs** - Uses StackExchange API, PyPI API, GitHub raw URLs (not web scraping)
✅ **Zero Dependencies** - Uses Python standard library (`urllib`)
✅ **File Operations** - Read, write, and list files/directories
✅ **Code Extraction** - Automatically extracts code snippets from Stack Overflow
✅ **Multi-Source Research** - Combines PyPI, GitHub, Stack Overflow, and docs
✅ **Formatted Output** - Returns markdown-formatted, readable responses
✅ **Error Handling** - Graceful fallbacks for failed requests

## Tool Comparison

| Feature | search_stackoverflow | get_github_readme | search_pypi | get_comprehensive_info |
|---------|---------------------|-------------------|-------------|----------------------|
| API Used | StackExchange API | GitHub raw files | PyPI JSON | All sources |
| Auth Required | No | No | No | No |
| Rate Limits | 300/day (no key) | None | None | Combined |
| Reliability | ✅ High | ✅ High | ✅ High | ✅ High |

## Troubleshooting

**"No results found"**
- Check your internet connection
- Verify the package/repo name is correct
- Some packages may not have GitHub repos or PyPI entries

**"Error: Tool call failed"**
- Check the server logs (printed to stderr)
- Verify tool parameters match the schema
- Ensure file paths exist (for file tools)

**"HTTP Error 403/404"**
- 404: Resource not found (wrong package/repo name)
- 403: Rate limited (wait a few minutes)

**Rate Limits:**
- StackExchange API: 300 requests/day without auth key
- PyPI: No rate limits
- GitHub raw: No rate limits

## File Structure

```
agents/
├── coding_server.py          # MCP server implementation
├── coding_tools.py           # WebResearchToolkit implementation
└── README_CODING_SERVER.md   # This file
```

## Advanced Usage

### Custom Search Queries

For more specific results, use descriptive queries:

```json
// Generic
{"query": "python requests"}

// Better
{"query": "python requests post json authentication"}
```

### Combining Tools

1. Search for package: `search_pypi("package-name")`
2. Get README: `get_github_readme("package-name")`
3. Find examples: `find_usage_examples("package-name tutorial")`
4. Save example: `write_file(path="example.py", content=code)`

### Directory Operations

```json
// List current directory
{"directory": "."}

// List specific directory
{"directory": "/home/user/projects"}

// List parent directory
{"directory": ".."}
```

## Extending the Server

To add new tools:

1. Add method to `WebResearchToolkit` (in `coding_tools.py`)
2. Register tool in `@app.list_tools()` (in `coding_server.py`)
3. Add handler in `@app.call_tool()` (in `coding_server.py`)

Example:
```python
# In coding_tools.py
def new_feature(self, param):
    # Implementation
    return result

# In coding_server.py - list_tools()
types.Tool(
    name="new_feature",
    description="Does something cool",
    inputSchema={...}
)

# In coding_server.py - call_tool()
elif name == "new_feature":
    result = web_toolkit.new_feature(arguments.get("param"))
```

## Security Notes

⚠️ **File Operations**: The file tools can read/write anywhere the server has permissions. Use with caution.
✅ **Web Research**: All web requests use official APIs - no authentication needed
✅ **No Sensitive Data**: No API keys or credentials stored in code

## FAQ

**Q: Do I need API keys?**
A: No! All tools use public APIs or unauthenticated endpoints.

**Q: Can I use this offline?**
A: File tools work offline. Web research tools require internet.

**Q: What's the difference from coder_server?**
A: coding_server uses official APIs (more reliable), has 8 tools vs 3, and includes file write operations.

**Q: How do I increase StackOverflow rate limits?**
A: Register for a StackExchange API key and modify line 35 in `coding_tools.py`:
```python
url = f"...&key={YOUR_API_KEY}"
```

## Contributing

This is part of the ece153a homework/project. Feel free to extend with:
- Additional package managers (npm, cargo, etc.)
- More code extraction patterns
- Documentation search improvements
- Caching for repeated queries

---

**Built with**: Python 3.7+ | MCP Protocol | StackExchange API | PyPI API | GitHub
**Files**: `coding_server.py`, `coding_tools.py`
**Dependencies**: None (uses standard library)
