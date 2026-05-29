#!/usr/bin/env python3
"""
AI Screenwriting Professor - Agentic Edition

Features:
- Agentic tool use (model decides what context it needs)
- Multi-file support
- Deep analysis mode
- Respects writer's voice
- Persistent sessions
"""

import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()


class FountainParser:
    @staticmethod
    def parse(content: str):
        lines = content.split('\n')
        scenes, characters = [], set()
        current_scene, scene_number = None, 1
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            if re.match(r'^(INT\.|EXT\.|INT/EXT)', line, re.IGNORECASE):
                if current_scene:
                    scenes.append(current_scene)
                current_scene = {
                    "number": scene_number,
                    "heading": line,
                    "content": [],
                    "characters": set()
                }
                scene_number += 1
            
            elif line.isupper() and 0 < len(line) < 40:
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line and not next_line.isupper():
                        char_name = line.split('(')[0].strip()
                        characters.add(char_name)
                        if current_scene:
                            current_scene["characters"].add(char_name)
            
            if current_scene and line:
                current_scene["content"].append(line)
        
        if current_scene:
            scenes.append(current_scene)
        
        for scene in scenes:
            scene["characters"] = list(scene["characters"])
        
        total_words = sum(len(" ".join(s["content"]).split()) for s in scenes)
        
        return {
            "scenes": scenes,
            "characters": list(characters),
            "total_scenes": len(scenes),
            "total_characters": len(characters),
            "estimated_pages": int(total_words / 250)
        }


class ScreenwritingProfessor:
    def __init__(self, project_name: str = "my_script"):
        base = Path(os.environ.get("SANDBOX_DATA_DIR", ".")) / "screenwrite" / "projects"
        self.project_dir = base / project_name
        self.project_dir.mkdir(parents=True, exist_ok=True)
        
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in .env file")
        
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-opus-4-5-20251101"
        
        print(f"✓ {self.model} loaded")
        print(f"✓ Project: {project_name}\n")
        
        # Main screenplay
        self.screenplay_content = ""
        self.screenplay_name = ""
        self.scenes = []
        self.characters = []
        
        # Additional files
        self.additional_files = {}
        
        # Analysis storage
        self.character_analyses = {}
        self.scene_analyses = {}
        self.action_items = []
        
        # Conversation
        self.conversation_history = []
        
        # User context
        self.user_context = {
            "genre": None,
            "concerns": [],
            "goals": [],
            "voice_notes": [],
        }
        
        self._load()
        
        # Define tools the agent can use
        self.tools = [
            {
                "name": "get_scene",
                "description": "Retrieve the full content of a specific scene by number. Use this when you need to read or analyze a scene.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scene_number": {
                            "type": "integer",
                            "description": "The scene number to retrieve"
                        }
                    },
                    "required": ["scene_number"]
                }
            },
            {
                "name": "get_character_scenes",
                "description": "Retrieve all scenes featuring a specific character. Use this when analyzing a character or their arc.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "character_name": {
                            "type": "string",
                            "description": "The character's name (as it appears in the screenplay)"
                        }
                    },
                    "required": ["character_name"]
                }
            },
            {
                "name": "get_scene_range",
                "description": "Retrieve multiple consecutive scenes. Use this for analyzing sequences, act breaks, or pacing.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "start_scene": {
                            "type": "integer",
                            "description": "First scene number"
                        },
                        "end_scene": {
                            "type": "integer",
                            "description": "Last scene number"
                        }
                    },
                    "required": ["start_scene", "end_scene"]
                }
            },
            {
                "name": "get_additional_file",
                "description": "Retrieve an additional file (alternate scene, notes, outline, etc.) by its label.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "The label of the additional file"
                        }
                    },
                    "required": ["label"]
                }
            },
            {
                "name": "list_scenes",
                "description": "Get a list of all scenes with their headings. Use this to understand the screenplay structure or find specific scenes.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "list_characters",
                "description": "Get a list of all characters and how many scenes they appear in.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "list_additional_files",
                "description": "Get a list of all additional files that have been loaded.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "search_dialogue",
                "description": "Search for specific words or phrases in dialogue throughout the screenplay.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The word or phrase to search for"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_full_screenplay",
                "description": "Retrieve the entire screenplay. Only use this when you need to analyze the whole script (structure, pacing, overall arc). For specific scenes or characters, use the more targeted tools.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]
    
    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the result."""
        
        if tool_name == "get_scene":
            scene_num = tool_input["scene_number"]
            for scene in self.scenes:
                if scene["number"] == scene_num:
                    content = f"SCENE {scene_num}: {scene['heading']}\n"
                    content += f"Characters in scene: {', '.join(scene['characters'])}\n\n"
                    content += "\n".join(scene['content'])
                    return content
            return f"Scene {scene_num} not found. The screenplay has {len(self.scenes)} scenes."
        
        elif tool_name == "get_character_scenes":
            char_name = tool_input["character_name"]
            # Try exact match first, then case-insensitive
            matched_name = None
            for c in self.characters:
                if c == char_name:
                    matched_name = c
                    break
                if c.lower() == char_name.lower():
                    matched_name = c
            
            if not matched_name:
                return f"Character '{char_name}' not found. Available characters: {', '.join(self.characters)}"
            
            char_scenes = [s for s in self.scenes if matched_name in s["characters"]]
            if not char_scenes:
                return f"{matched_name} doesn't appear in any scenes."
            
            result = f"{matched_name} appears in {len(char_scenes)} scenes:\n\n"
            for s in char_scenes:
                result += f"--- SCENE {s['number']}: {s['heading']} ---\n"
                result += "\n".join(s['content'])
                result += "\n\n"
            return result
        
        elif tool_name == "get_scene_range":
            start = tool_input["start_scene"]
            end = tool_input["end_scene"]
            result = f"SCENES {start}-{end}:\n\n"
            found = False
            for scene in self.scenes:
                if start <= scene["number"] <= end:
                    found = True
                    result += f"--- SCENE {scene['number']}: {scene['heading']} ---\n"
                    result += f"Characters: {', '.join(scene['characters'])}\n\n"
                    result += "\n".join(scene['content'])
                    result += "\n\n"
            if not found:
                return f"No scenes found in range {start}-{end}. The screenplay has {len(self.scenes)} scenes."
            return result
        
        elif tool_name == "get_additional_file":
            label = tool_input["label"]
            # Try exact match, then case-insensitive
            matched_label = None
            for l in self.additional_files.keys():
                if l == label:
                    matched_label = l
                    break
                if l.lower() == label.lower():
                    matched_label = l
            
            if matched_label:
                data = self.additional_files[matched_label]
                return f"FILE: {matched_label} ({data['filename']})\n\n{data['content']}"
            return f"No file labeled '{label}'. Available files: {', '.join(self.additional_files.keys()) or 'none'}"
        
        elif tool_name == "list_scenes":
            if not self.scenes:
                return "No screenplay loaded."
            result = f"SCREENPLAY: {self.screenplay_name}\n{len(self.scenes)} scenes:\n\n"
            for s in self.scenes:
                chars = f" [{', '.join(s['characters'][:3])}]" if s['characters'] else ""
                result += f"  {s['number']:3}. {s['heading']}{chars}\n"
            return result
        
        elif tool_name == "list_characters":
            if not self.characters:
                return "No screenplay loaded."
            result = "CHARACTERS:\n\n"
            for c in self.characters:
                count = sum(1 for s in self.scenes if c in s["characters"])
                result += f"  {c}: {count} scenes\n"
            return result
        
        elif tool_name == "list_additional_files":
            if not self.additional_files:
                return "No additional files loaded."
            result = "ADDITIONAL FILES:\n\n"
            for label, data in self.additional_files.items():
                result += f"  '{label}' ({data['filename']})\n"
            return result
        
        elif tool_name == "search_dialogue":
            query = tool_input["query"].lower()
            results = []
            for scene in self.scenes:
                for i, line in enumerate(scene['content']):
                    if query in line.lower():
                        # Get some context
                        start = max(0, i - 2)
                        end = min(len(scene['content']), i + 3)
                        context = "\n".join(scene['content'][start:end])
                        results.append(f"Scene {scene['number']} ({scene['heading']}):\n{context}")
            
            if not results:
                return f"No matches found for '{query}'."
            return f"Found {len(results)} matches for '{query}':\n\n" + "\n\n---\n\n".join(results[:10])
        
        elif tool_name == "get_full_screenplay":
            if not self.screenplay_content:
                return "No screenplay loaded."
            return f"FULL SCREENPLAY: {self.screenplay_name}\n\n{self.screenplay_content}"
        
        return f"Unknown tool: {tool_name}"
    
    def _load(self):
        data_file = self.project_dir / "data.json"
        if data_file.exists():
            data = json.loads(data_file.read_text())
            self.scenes = data.get("scenes", [])
            self.characters = data.get("characters", [])
            self.screenplay_name = data.get("screenplay_name", "")
            self.additional_files = data.get("additional_files", {})
            self.character_analyses = data.get("character_analyses", {})
            self.scene_analyses = data.get("scene_analyses", {})
            self.action_items = data.get("action_items", [])
            self.user_context = data.get("user_context", self.user_context)
            print("✓ Loaded saved data")
            
            if self.additional_files:
                print(f"✓ {len(self.additional_files)} additional files")
        
        history_file = self.project_dir / "history.json"
        if history_file.exists():
            self.conversation_history = json.loads(history_file.read_text())
            count = len([m for m in self.conversation_history if m["role"] == "user"])
            print(f"✓ {count} previous conversations loaded")
        
        screenplay_file = self.project_dir / "screenplay.fountain"
        if screenplay_file.exists():
            self.screenplay_content = screenplay_file.read_text()
            print("✓ Main screenplay cached")
    
    def _save(self):
        data = {
            "scenes": self.scenes,
            "characters": self.characters,
            "screenplay_name": self.screenplay_name,
            "additional_files": self.additional_files,
            "character_analyses": self.character_analyses,
            "scene_analyses": self.scene_analyses,
            "action_items": self.action_items,
            "user_context": self.user_context
        }
        (self.project_dir / "data.json").write_text(json.dumps(data, indent=2))
        (self.project_dir / "history.json").write_text(
            json.dumps(self.conversation_history, indent=2)
        )
        if self.screenplay_content:
            (self.project_dir / "screenplay.fountain").write_text(self.screenplay_content)
    
    def _build_system_prompt(self):
        context_parts = []
        
        if self.screenplay_content:
            context_parts.append(
                f"MAIN SCREENPLAY: '{self.screenplay_name}' "
                f"({len(self.scenes)} scenes, {len(self.characters)} characters)"
            )
        
        if self.additional_files:
            file_list = [f"'{label}'" for label in self.additional_files.keys()]
            context_parts.append(f"ADDITIONAL FILES: {', '.join(file_list)}")
        
        if self.user_context.get("genre"):
            context_parts.append(f"GENRE: {self.user_context['genre']}")
        if self.user_context.get("concerns"):
            context_parts.append(
                f"WRITER'S CONCERNS: {', '.join(self.user_context['concerns'])}"
            )
        if self.user_context.get("goals"):
            context_parts.append(
                f"WRITER'S GOALS: {', '.join(self.user_context['goals'])}"
            )
        if self.user_context.get("voice_notes"):
            context_parts.append(
                f"WRITER'S VOICE/STYLE: {', '.join(self.user_context['voice_notes'])}"
            )
        
        system = """You are a warm, experienced screenwriting professor with deep respect for each writer's unique voice. Your student can't afford film school - you are their complete education.

YOU HAVE TOOLS - USE THEM:
You have access to tools that let you read scenes, find characters, search dialogue, and more. USE THESE TOOLS to ground your feedback in their actual script. Don't give generic advice - pull up the specific scenes and reference specific lines.

When analyzing anything:
1. First use the appropriate tools to get the actual content
2. Then give feedback grounded in what you've read
3. Reference specific lines, moments, and choices

For example:
- "Let me look at that scene..." → use get_scene
- "I'll trace Sarah's arc..." → use get_character_scenes  
- "Let me check the pacing of act 2..." → use get_scene_range
- "How does that compare to your alternate version?" → use get_additional_file

YOUR PHILOSOPHY:
The goal is NOT to make every script sound the same. Hollywood already has enough generic, committee-approved screenplays. Your job is to help THIS writer tell THEIR story more effectively, in THEIR voice. Some of the best films broke "rules" - your student might be onto something. Stay curious.

RESPECTING THE WRITER'S VOICE:
- Their instincts brought them this far. Trust that they have reasons for their choices.
- Before suggesting changes, ASK: "What were you going for here?" or "Tell me about this choice."
- If something unusual is working, SAY SO. Don't fix what isn't broken.
- Distinguish between "this breaks a rule" and "this isn't working."
- When you see a distinctive choice, get curious about their intent before judging it.
- Some writers are sparse. Some are lyrical. Some are weird. Help them be MORE themselves.
- If their instinct is good, tell them to trust it.

WHEN TO PUSH BACK VS. SUPPORT:
- Push back when: something is unclear, a choice undermines their stated goals, craft fundamentals are missing
- Support when: a choice is unconventional but intentional, their voice is distinctive, they're taking a risk
- Ask questions when: you're not sure which category something falls into

YOUR TEACHING STYLE:
- Be conversational and encouraging, like a mentor who genuinely loves movies
- ASK QUESTIONS to understand their vision before giving feedback
- Explain the WHY behind craft principles - but also when those principles can bend
- Reference films that broke conventions successfully
- Be honest but constructive
- Remember what they've told you and build on it

RESPONSE DEPTH:
- Give thorough, detailed feedback grounded in THEIR actual script
- When analyzing a scene: go beat by beat, discuss what's working and why
- When discussing a character: trace their arc, reference specific moments
- Reference their actual lines and moments, not just general advice
- It's fine to write several paragraphs when analyzing something

BE CONVERSATIONAL:
- After giving feedback, ask if they want to dig deeper or move on
- Check in: "Does this resonate?" or "What's your gut saying?"

THINGS YOU SHOULD NEVER DO:
- Don't give feedback without first using tools to read the actual content
- Don't sand off the edges that make their script distinctive
- Don't assume every unconventional choice is a mistake
- Don't give generic advice that could apply to any script
- Don't rewrite their voice into something more "normal"
- Don't pile on criticism without acknowledging what's working"""

        if context_parts:
            system += "\n\nCURRENT PROJECT CONTEXT:\n" + "\n".join(context_parts)
        
        if self.characters:
            system += f"\n\nCHARACTERS IN SCREENPLAY: {', '.join(self.characters[:15])}"
        
        return system
    
    def read_fountain(self, file_path: str) -> str:
        """Load the main screenplay."""
        screenplay_file = Path(file_path)
        if not screenplay_file.exists():
            return f"I couldn't find that file: {file_path}. Could you check the path?"
        
        self.screenplay_content = screenplay_file.read_text(encoding='utf-8')
        self.screenplay_name = screenplay_file.name
        
        parsed = FountainParser.parse(self.screenplay_content)
        self.scenes = parsed["scenes"]
        self.characters = parsed["characters"]
        self._save()
        
        char_preview = ', '.join(self.characters[:8])
        if len(self.characters) > 8:
            char_preview += '...'
        
        return f"""I've loaded **{self.screenplay_name}** as your main screenplay.

Here's what I found:
- **{parsed['estimated_pages']} pages** (estimated)
- **{parsed['total_scenes']} scenes**
- **{parsed['total_characters']} characters**: {char_preview}

Before we dive in, I'd love to understand your vision:
- What's the genre or tone you're going for?
- What excites you most about this story?
- Is there anything you want me to NOT mess with?

(You can also load additional files with `add <file> as <label>`)"""
    
    def add_file(self, file_path: str, label: str = None) -> str:
        """Load an additional file."""
        f = Path(file_path)
        if not f.exists():
            return f"Couldn't find: {file_path}"
        
        content = f.read_text(encoding='utf-8')
        label = label or f.stem
        
        parsed_info = ""
        if f.suffix.lower() == '.fountain' or 'INT.' in content or 'EXT.' in content:
            parsed = FountainParser.parse(content)
            if parsed['scenes']:
                parsed_info = f" ({len(parsed['scenes'])} scenes)"
        
        self.additional_files[label] = {
            "content": content,
            "filename": f.name
        }
        self._save()
        
        return f"""Got it - loaded '{f.name}' as **{label}**{parsed_info}.

Just mention "{label}" and I'll pull it up to compare or discuss."""
    
    def remove_file(self, label: str) -> str:
        """Remove an additional file."""
        if label in self.additional_files:
            filename = self.additional_files[label]['filename']
            del self.additional_files[label]
            self._save()
            return f"Removed '{label}' ({filename})."
        return f"No file labeled '{label}'."
    
    def list_files(self) -> str:
        """List all loaded files."""
        lines = []
        
        if self.screenplay_name:
            lines.append(f"  📄 **{self.screenplay_name}** (main)")
            lines.append(f"     {len(self.scenes)} scenes, {len(self.characters)} characters")
        
        if self.additional_files:
            lines.append("\n  Additional:")
            for label, data in self.additional_files.items():
                lines.append(f"  📎 **{label}** ({data['filename']})")
        
        if not lines:
            return "No files loaded. Use `read <file>` for your main screenplay."
        
        return "**Loaded files:**\n\n" + "\n".join(lines)
    
    def chat(self, user_message: str) -> str:
        """Main conversational interface with agentic tool use."""
        lower_msg = user_message.lower().strip()
        
        # === SIMPLE COMMANDS (no agent needed) ===
        
        if lower_msg.startswith("read "):
            file_path = user_message[5:].strip()
            result = self.read_fountain(file_path)
            self.conversation_history.append({"role": "user", "content": f"[Loaded: {file_path}]"})
            self.conversation_history.append({"role": "assistant", "content": result})
            self._save()
            return result
        
        if lower_msg.startswith("add "):
            parts = user_message[4:].strip().split(" as ")
            file_path = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else None
            result = self.add_file(file_path, label)
            self._save()
            return result
        
        if lower_msg.startswith("remove "):
            return self.remove_file(user_message[7:].strip())
        
        if lower_msg == "files":
            return self.list_files()
        
        if lower_msg == "clear context":
            self.user_context = {"genre": None, "concerns": [], "goals": [], "voice_notes": []}
            self._save()
            return "Cleared stored context."
        
        # === AGENTIC CONVERSATION ===
        
        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})
        
        # Keep history manageable
        if len(self.conversation_history) > 80:
            self.conversation_history = self.conversation_history[-80:]
        
        system_prompt = self._build_system_prompt()
        
        print("\033[1;32mProfessor:\033[0m ", end="", flush=True)
        
        full_response = ""
        messages = self.conversation_history.copy()
        
        # Agentic loop - keep going until the model stops calling tools
        max_iterations = 10
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8192,
                system=system_prompt,
                tools=self.tools,
                messages=messages
            )
            
            # Check if we have tool use
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            text_blocks = [block for block in response.content if block.type == "text"]
            
            # Print any text
            for block in text_blocks:
                print(block.text, end="", flush=True)
                full_response += block.text
            
            # If no tool calls, we're done
            if not tool_uses:
                break
            
            # Process tool calls
            # Add assistant's response to messages
            messages.append({"role": "assistant", "content": response.content})
            
            # Execute each tool and add results
            tool_results = []
            for tool_use in tool_uses:
                print(f"\n\033[1;33m  [Reading: {tool_use.name}...]\033[0m", end="", flush=True)
                
                result = self._execute_tool(tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result
                })
            
            messages.append({"role": "user", "content": tool_results})
            print()  # Newline after tool calls
        
        print()  # Final newline
        
        # Save the final response to history
        self.conversation_history.append({"role": "assistant", "content": full_response})
        self._extract_context(user_message)
        self._save()
        
        return ""
    
    def _extract_context(self, user_msg: str):
        """Learn from what user tells us."""
        lower = user_msg.lower()
        
        genres = ['thriller', 'comedy', 'drama', 'horror', 'romance', 'action', 
                  'sci-fi', 'fantasy', 'mystery', 'western', 'noir', 'heist',
                  'coming-of-age', 'biopic', 'war', 'sports', 'musical', 
                  'dark comedy', 'satire', 'psychological', 'supernatural',
                  'crime', 'adventure', 'indie', 'arthouse', 'experimental']
        for genre in genres:
            if genre in lower and not self.user_context.get("genre"):
                self.user_context["genre"] = genre
                break
        
        concern_phrases = ['worried about', 'concerned about', 'struggling with', 
                         'not sure about', 'problem with', 'stuck on']
        for phrase in concern_phrases:
            if phrase in lower:
                idx = lower.find(phrase) + len(phrase)
                concern = user_msg[idx:idx+100].split('.')[0].strip()
                if concern and concern not in self.user_context["concerns"]:
                    self.user_context["concerns"].append(concern)
                break
        
        goal_phrases = ['i want', 'trying to', 'hoping to', 'goal is', 
                       'aiming for', 'going for', 'should feel like']
        for phrase in goal_phrases:
            if phrase in lower:
                idx = lower.find(phrase) + len(phrase)
                goal = user_msg[idx:idx+100].split('.')[0].strip()
                if goal and goal not in self.user_context["goals"]:
                    self.user_context["goals"].append(goal)
                break
        
        voice_phrases = ["don't change", "keep the", "i like the", "intentionally", 
                        "supposed to be", "meant to", "my style is"]
        for phrase in voice_phrases:
            if phrase in lower:
                idx = lower.find(phrase)
                note = user_msg[idx:idx+100].split('.')[0].strip()
                if note and note not in self.user_context.get("voice_notes", []):
                    if "voice_notes" not in self.user_context:
                        self.user_context["voice_notes"] = []
                    self.user_context["voice_notes"].append(note)
                break


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("\nUsage: python screenplay_professor.py <project_name>")
        print("Example: python screenplay_professor.py my_thriller\n")
        sys.exit(1)
    
    prof = ScreenwritingProfessor(sys.argv[1])
    
    print("=" * 70)
    print("🎬 AI SCREENWRITING PROFESSOR (Agentic)")
    print("=" * 70)
    
    print("""
Welcome! I'm your screenwriting professor. I'll read your actual 
scenes and give you feedback grounded in YOUR script.

COMMANDS:
   read <file>              Load main screenplay
   add <file> as <label>    Add extra file
   remove <label>           Remove extra file
   files                    List loaded files
   clear context            Reset stored preferences
   quit                     Exit

Just talk naturally - I'll pull up whatever I need:
   "What do you think of scene 5?"
   "How's Marcus's arc working?"
   "Compare scene 12 with the alternate ending"
   "Is the pacing in act 2 too slow?"
""")
    print("=" * 70)
    
    if not prof.screenplay_content:
        print("\n\033[1;32mProfessor:\033[0m No screenplay loaded yet.")
        print("Use `read <filename>` to load your script.\n")
    else:
        print(f"\n\033[1;32mProfessor:\033[0m Welcome back! Working on '{prof.screenplay_name}'.")
        print("What would you like to focus on today?\n")
    
    while True:
        try:
            user_input = input("\033[1;36mYou:\033[0m ").strip()
            if not user_input:
                continue
            
            if user_input.lower() == "quit":
                print("\n\033[1;32mProfessor:\033[0m Trust your instincts. Keep writing.\n")
                break
            
            result = prof.chat(user_input)
            if result:
                print(f"\033[1;32mProfessor:\033[0m {result}")
            print()
        
        except KeyboardInterrupt:
            print("\n\n\033[1;32mProfessor:\033[0m See you next time!\n")
            break
        except Exception as e:
            print(f"\n\033[1;31mError:\033[0m {e}\n")


if __name__ == "__main__":
    main()