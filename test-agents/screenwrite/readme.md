# 🎬 AI Screenwriting Professor

An intelligent screenwriting coach that reads your actual script and provides personalized feedback grounded in your specific scenes, characters, and choices. Think of it as having an experienced film school professor who respects your voice and can't be fooled by vague questions.

## What Makes This Different

**Agentic Tool Use**: The AI doesn't just give generic screenwriting advice. It actively reads your scenes, traces character arcs, analyzes pacing, and references your actual dialogue before responding.

**Respects Your Voice**: Not every script should sound the same. This professor helps you tell YOUR story better, not rewrite it into a Hollywood template.

**Persistent Memory**: Remembers your genre, concerns, goals, and stylistic preferences across sessions.

**Multi-File Support**: Compare alternate scenes, reference your outline, or load beat sheets alongside your main screenplay.

## Features

- 📖 **Scene-by-scene analysis** with specific line references
- 🎭 **Character arc tracking** across all appearances
- 🔍 **Dialogue search** to find patterns or repeated phrases
- 📊 **Pacing analysis** for acts and sequences
- 💾 **Session persistence** - pick up where you left off
- 🎯 **Goal-oriented feedback** based on what you tell it

## Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd screenplay-professor

# Install dependencies
pip install anthropic python-dotenv

# Set up your API key
echo "ANTHROPIC_API_KEY=your-key-here" > .env
```

## Quick Start

```bash
python screenplay_professor.py my_thriller
```

Then load your screenplay:
```
You: read my_script.fountain
```

## Usage

### Commands

```
read <file>              Load your main screenplay
add <file> as <label>    Add alternate scenes, outlines, notes
remove <label>           Remove an additional file
files                    List all loaded files
clear context            Reset stored preferences
quit                     Exit
```

### Natural Conversation

Just ask questions naturally - the AI will pull up whatever it needs:

```
You: What do you think of scene 5?
Professor: [Reads scene 5] Let me look at that...

You: How's Marcus's arc working?
Professor: [Reads all Marcus scenes] I'll trace his journey...

You: Is the pacing in act 2 too slow?
Professor: [Reads scenes 20-45] Let me check the rhythm...

You: Compare scene 12 with the alternate ending
Professor: [Reads both files] Interesting contrast here...
```

## How It Works

The agent has access to these tools:
- `get_scene` - Read a specific scene
- `get_character_scenes` - All scenes featuring a character
- `get_scene_range` - Multiple consecutive scenes
- `get_additional_file` - Alternate versions, notes, outlines
- `list_scenes` - Overview of structure
- `list_characters` - Who appears and how often
- `search_dialogue` - Find specific phrases
- `get_full_screenplay` - Complete script (used sparingly)

The AI decides which tools to use based on your question, reads the actual content, then gives feedback grounded in what it found.

## Project Structure

```
projects/
  └── my_thriller/
      ├── screenplay.fountain    # Your main script
      ├── data.json             # Parsed scenes, characters, analyses
      └── history.json          # Conversation history
```

## Supported Formats

- **Fountain** (.fountain) - Primary format
- **Plain text** with standard screenplay formatting
- The parser recognizes: INT./EXT. scene headings, CHARACTER NAMES in caps, dialogue

## Example Session

```
You: read darklight.fountain

Professor: I've loaded darklight.fountain as your main screenplay.
- 98 pages (estimated)
- 67 scenes
- 8 characters: MAYA, DEREK, THE ARCHITECT, SARAH...

Before we dive in, I'd love to understand your vision:
- What's the genre or tone you're going for?
- What excites you most about this story?

You: It's a psychological thriller. I'm worried the twist in scene 45 is too obvious.

Professor: [Reads scene 45] Let me look at that scene...
[Provides detailed analysis of the specific scene, references earlier setup, 
discusses whether the foreshadowing is balanced]

Does this resonate? Want me to trace back the clues you've planted?
```

## Philosophy

This tool is built on the belief that:
- Generic screenwriting advice is useless
- Every writer has a unique voice worth preserving
- The best feedback comes from reading the actual work
- Rules can be broken if you know why
- Questions matter more than pronouncements

## Requirements

- Python 3.7+
- Anthropic API key (Claude Opus 4.5)
- `anthropic` package
- `python-dotenv` package

## Tips

**Be specific**: "What do you think?" is vague. "Is Derek's motivation clear in the interrogation scene?" gets better feedback.

**Load everything**: Add your outline, alternate scenes, or notes with `add <file> as <label>` so the professor can reference them.

**Save tokens**: The agent only loads what it needs. Don't worry about asking broad questions - it'll fetch specific scenes rather than reading everything.

**Trust your gut**: If the professor suggests something that feels wrong for your story, say so. It's designed to respect your instincts.

## License

MIT

## Contributing

This is a teaching tool. If you have ideas for better ways to analyze screenplays or respect writer's voices, pull requests welcome.

---

**Note**: This tool uses Claude Opus 4.5 which requires an Anthropic API key. Usage costs apply based on Anthropic's pricing.
