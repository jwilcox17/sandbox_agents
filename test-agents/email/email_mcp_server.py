import asyncio
import email
import imaplib
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from email.header import decode_header
from email.utils import parsedate_tz, mktime_tz
from typing import List, Dict, Any, Optional

import httpx
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
MAX_BODY_LENGTH = 2000
MAX_LLM_BODY_LENGTH = 500
MAX_PREVIEW_LENGTH = 200
DEFAULT_TIMEOUT = 30.0

# Gmail IMAP configuration (hardcoded since Gmail settings are standard)
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

# Email credentials from environment variables
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "jwilcox.email@gmail.com")
APP_PASSWORD = os.getenv("APP_PASSWORD", "ijexywomspgwpmvx")

# LLM Configuration (using same environment variables as client)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")

class EmailMessage(BaseModel):
    """Email message data structure"""
    uid: str
    subject: str
    sender: str
    date: str
    body: str
    has_attachments: bool = False
    is_read: bool = False
    date_info: Optional[Dict[str, Any]] = None

class EmailSummary(BaseModel):
    """Email summary data structure"""
    category: str
    priority: str
    summary: str
    action_required: bool = False

class GmailConnector:
    """Gmail IMAP connection handler"""
    
    def __init__(self):
        self.mail = None
        self.connected = False
    
    async def connect(self) -> bool:
        """Connect to Gmail IMAP server"""
        try:
            self.mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            result = self.mail.login(EMAIL_ADDRESS, APP_PASSWORD)
            
            if result[0] == 'OK':
                self.mail.select('inbox')
                self.connected = True
                logger.info("Connected to Gmail successfully")
                return True
            else:
                logger.error(f"Gmail login failed: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Gmail connection error: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from Gmail"""
        if self.mail and self.connected:
            try:
                self.mail.close()
                self.mail.logout()
                self.connected = False
                logger.info("Disconnected from Gmail")
            except Exception as e:
                logger.error(f"Error disconnecting from Gmail: {e}")
    
    @staticmethod
    def decode_mime_header(header_value: str) -> str:
        """Decode MIME encoded header"""
        if not header_value:
            return ""
        
        try:
            decoded_parts = decode_header(header_value)
            decoded_string = ""
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    decoded_string += part.decode(encoding or 'utf-8')
                else:
                    decoded_string += part
            return decoded_string
        except Exception as e:
            logger.error(f"Error decoding header '{header_value}': {e}")
            return str(header_value)
    
    @staticmethod
    def extract_email_body(msg) -> str:
        """Extract plain text body from email message"""
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            return payload.decode('utf-8', errors='ignore').strip()
            else:
                if msg.get_content_type() == "text/plain":
                    payload = msg.get_payload(decode=True)
                    if payload:
                        return payload.decode('utf-8', errors='ignore').strip()
                        
        except Exception as e:
            logger.error(f"Error extracting email body: {e}")
            return "Error reading email body"
            
        return ""
    
    async def fetch_last_emails(self, count: int = 30) -> List[EmailMessage]:
        """Fetch the last N emails from inbox"""
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            # Search for all emails, sorted by date (newest first)
            status, messages = self.mail.search(None, 'ALL')
            
            if status != 'OK':
                logger.error("Failed to search emails")
                return []
            
            # Get message UIDs
            message_uids = messages[0].split()
            
            # Take the last N messages (most recent)
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()  # Newest first
            
            for uid in recent_uids:
                try:
                    # Fetch email message
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    # Parse email
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Extract email details
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    # Parse date with enhanced analysis
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    # Extract body
                    body = self.extract_email_body(msg)
                    
                    # Check for attachments
                    has_attachments = self._has_attachments(msg)
                    
                    # Create email object with enhanced date info
                    email_obj = EmailMessage(
                        uid=uid.decode('utf-8'),
                        subject=subject,
                        sender=sender,
                        date=date_formatted,
                        body=body[:MAX_BODY_LENGTH],
                        has_attachments=has_attachments,
                        is_read=False,  # Would need additional IMAP call to check
                        date_info=date_info
                    )
                    
                    emails.append(email_obj)
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            logger.info(f"Successfully fetched {len(emails)} emails")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching emails: {e}")
            return []
    
    async def fetch_emails_by_date(self, target_date: str, date_range: str = "on") -> List[EmailMessage]:
        """
        Fetch emails from a specific date or date range
        
        Args:
            target_date: Date in YYYY-MM-DD format
            date_range: "on" (exact date), "since" (from date onwards), "before" (before date)
        """
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            # Convert date to IMAP format (DD-Mon-YYYY)
            try:
                parsed_date = datetime.strptime(target_date, "%Y-%m-%d")
                imap_date = parsed_date.strftime("%d-%b-%Y")
            except ValueError:
                logger.error(f"Invalid date format: {target_date}. Use YYYY-MM-DD format.")
                return []
            
            # Build IMAP search query based on date range
            if date_range == "on":
                search_query = f'ON {imap_date}'
            elif date_range == "since":
                search_query = f'SINCE {imap_date}'
            elif date_range == "before":
                search_query = f'BEFORE {imap_date}'
            else:
                logger.error(f"Invalid date_range: {date_range}. Use 'on', 'since', or 'before'.")
                return []
            
            # Search for emails with date criteria
            status, messages = self.mail.search(None, search_query)
            
            if status != 'OK':
                logger.error(f"Failed to search emails with query: {search_query}")
                return []
            
            # Get message UIDs
            message_uids = messages[0].split()
            
            if not message_uids:
                logger.info(f"No emails found for date query: {search_query}")
                return []
            
            # Reverse to get newest first
            message_uids.reverse()
            
            # Process each email (similar to fetch_last_emails)
            for uid in message_uids:
                try:
                    # Fetch email message
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    # Parse email
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Extract email details
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    # Parse date with enhanced analysis
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    # Extract body
                    body = self.extract_email_body(msg)
                    
                    # Check for attachments
                    has_attachments = self._has_attachments(msg)
                    
                    # Create email object with enhanced date info
                    email_obj = EmailMessage(
                        uid=uid.decode('utf-8'),
                        subject=subject,
                        sender=sender,
                        date=date_formatted,
                        body=body[:MAX_BODY_LENGTH],
                        has_attachments=has_attachments,
                        is_read=False,
                        date_info=date_info
                    )
                    
                    emails.append(email_obj)
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            logger.info(f"Successfully fetched {len(emails)} emails for date query: {search_query}")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching emails by date: {e}")
            return []
    
    @staticmethod
    def _parse_email_date(date_str: str) -> Dict[str, Any]:
        """Parse email date string to formatted date with additional analysis"""
        result = {
            "formatted_date": date_str,
            "timestamp": None,
            "datetime_obj": None,
            "timezone": None,
            "age_hours": None,
            "age_days": None,
            "is_recent": False,
            "is_today": False,
            "is_weekend": False,
            "time_period": "unknown"  # morning, afternoon, evening, night
        }
        
        try:
            date_tuple = parsedate_tz(date_str)
            if date_tuple:
                timestamp = mktime_tz(date_tuple)
                dt = datetime.fromtimestamp(timestamp)
                now = datetime.now()
                
                # Basic formatting
                result["formatted_date"] = dt.strftime('%Y-%m-%d %H:%M:%S')
                result["timestamp"] = timestamp
                result["datetime_obj"] = dt
                
                # Timezone info
                if date_tuple[9] is not None:
                    tz_offset = date_tuple[9]
                    result["timezone"] = f"UTC{'+' if tz_offset >= 0 else ''}{tz_offset // 3600:02d}:{(abs(tz_offset) % 3600) // 60:02d}"
                
                # Age calculation
                age_delta = now - dt
                result["age_hours"] = age_delta.total_seconds() / 3600
                result["age_days"] = age_delta.days
                
                # Recency flags
                result["is_recent"] = age_delta.total_seconds() < 24 * 3600  # Less than 24 hours
                result["is_today"] = dt.date() == now.date()
                result["is_weekend"] = dt.weekday() >= 5  # Saturday=5, Sunday=6
                
                # Time period classification
                hour = dt.hour
                if 5 <= hour < 12:
                    result["time_period"] = "morning"
                elif 12 <= hour < 17:
                    result["time_period"] = "afternoon"
                elif 17 <= hour < 21:
                    result["time_period"] = "evening"
                else:
                    result["time_period"] = "night"
                    
        except Exception as e:
            logger.debug(f"Error parsing date '{date_str}': {e}")
        
        return result
    
    @staticmethod
    def _has_attachments(msg) -> bool:
        """Check if email message has attachments"""
        if not msg.is_multipart():
            return False
        
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                return True
        return False

class LLMSummarizer:
    """LLM-based email summarization and sorting"""
    
    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY
        self.model = ANTHROPIC_MODEL
    
    async def summarize_emails(self, emails: List[EmailMessage]) -> List[Dict[str, Any]]:
        """Summarize and categorize emails using LLM"""
        if not self.api_key:
            logger.warning("No Anthropic API key provided, skipping LLM summarization")
            return [{"error": "No API key provided for LLM summarization"}]
        
        summaries = []
        
        # Prepare email data for LLM with enhanced date analysis
        email_data = []
        for email in emails:
            email_info = {
                "subject": email.subject,
                "sender": email.sender,
                "date": email.date,
                "body": email.body[:MAX_LLM_BODY_LENGTH],
                "has_attachments": email.has_attachments
            }
            
            # Add date analysis if available
            if email.date_info:
                date_info = email.date_info
                email_info.update({
                    "is_recent": date_info.get("is_recent", False),
                    "is_today": date_info.get("is_today", False),
                    "is_weekend": date_info.get("is_weekend", False),
                    "time_period": date_info.get("time_period", "unknown"),
                    "age_hours": date_info.get("age_hours", None),
                    "age_days": date_info.get("age_days", None),
                    "timezone": date_info.get("timezone", None)
                })
            
            email_data.append(email_info)
        
        # Create prompt for LLM
        prompt = f"""
Analyze the following {len(emails)} emails and provide a summary and categorization for each:

{json.dumps(email_data, indent=2)}

For each email, provide:
1. Category (e.g., "Work", "Personal", "Marketing", "Newsletter", "Support", "Urgent")
2. Priority (High, Medium, Low) - consider time sensitivity (recent emails, today's emails may be higher priority)
3. Brief summary (1-2 sentences)
4. Action required (true/false)
5. Time_urgency ("immediate", "today", "this_week", "normal") - based on email timing and content

Return the response as a JSON array with this structure:
[
  {{
    "index": 0,
    "category": "Work",
    "priority": "High",
    "summary": "Brief summary of the email content",
    "action_required": true,
    "time_urgency": "immediate"
  }},
  ...
]
"""
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": self.api_key,
                        "anthropic-version": "2023-06-01"
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 4000,
                        "temperature": 0.1,
                        "messages": [
                            {"role": "user", "content": prompt}
                        ]
                    },
                    timeout=DEFAULT_TIMEOUT
                )
                
                if response.status_code == 200:
                    result = response.json()
                    content = result["content"][0]["text"]
                    
                    # Try to parse JSON response
                    summaries = self._parse_llm_response(content)
                            
                else:
                    logger.error(f"LLM API error: {response.status_code} - {response.text}")
                    summaries = [{"error": f"LLM API error: {response.status_code}"}]
                    
        except Exception as e:
            logger.error(f"Error in LLM summarization: {e}")
            summaries = [{"error": f"LLM summarization failed: {str(e)}"}]
        
        return summaries
    
    @staticmethod
    def _parse_llm_response(content: str) -> List[Dict[str, Any]]:
        """Parse LLM response, handling both direct JSON and embedded JSON"""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # If JSON parsing fails, try to extract JSON from text
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    pass
            return [{"error": "Failed to parse LLM response"}]

# Initialize components
gmail_connector = GmailConnector()
llm_summarizer = LLMSummarizer()

# Create MCP server
server = Server("email-mcp-server")

@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available tools"""
    return [
        Tool(
            name="mail_fetch_emails",
            description="Fetch the last N emails from Gmail inbox",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of emails to fetch (default: 30, max: 100)",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        Tool(
            name="mail_summarize_emails",
            description="Fetch emails and provide LLM-based summarization and categorization",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of emails to fetch and summarize (default: 30, max: 50)",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 100
                    }
                }
            }
        ),
        Tool(
            name="mail_get_email_stats",
            description="Get basic statistics about the email inbox",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="mail_find_emails_by_date",
            description="Find emails from a specific date or date range",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_date": {
                        "type": "string",
                        "description": "Target date in YYYY-MM-DD format (e.g., 2024-01-15)",
                        "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
                    },
                    "date_range": {
                        "type": "string",
                        "description": "Date range type: 'on' (exact date), 'since' (from date onwards), 'before' (before date)",
                        "enum": ["on", "since", "before"],
                        "default": "on"
                    }
                },
                "required": ["target_date"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    """Handle tool calls"""
    
    if name == "mail_fetch_emails":
        count = arguments.get("count", 30)
        
        try:
            emails = await gmail_connector.fetch_last_emails(count)
            
            if not emails:
                return [TextContent(type="text", text="No emails found or failed to connect to Gmail")]
            
            # Format emails for display
            result = f"Successfully fetched {len(emails)} emails:\n\n"
            
            for i, email in enumerate(emails, 1):
                result += f"{i}. Subject: {email.subject}\n"
                result += f"   From: {email.sender}\n"
                result += f"   Date: {email.date}\n"
                result += f"   Attachments: {'Yes' if email.has_attachments else 'No'}\n"
                result += f"   Body Preview: {email.body[:MAX_PREVIEW_LENGTH]}{'...' if len(email.body) > MAX_PREVIEW_LENGTH else ''}\n\n"
            
            return [TextContent(type="text", text=result)]
            
        except Exception as e:
            logger.error(f"Error in mail_fetch_emails: {e}")
            return [TextContent(type="text", text=f"Error fetching emails: {str(e)}")]
    
    elif name == "mail_summarize_emails":
        count = arguments.get("count", 30)
        
        try:
            # Fetch emails
            emails = await gmail_connector.fetch_last_emails(count)
            
            if not emails:
                return [TextContent(type="text", text="No emails found or failed to connect to Gmail")]
            
            # Get LLM summaries
            summaries = await llm_summarizer.summarize_emails(emails)
            
            # Prepare structured data for the client
            email_summaries = []
            categories = {}
            priorities = {}
            action_required_count = 0
            
            for i, email in enumerate(emails):
                email_data = {
                    "uid": email.uid,
                    "subject": email.subject,
                    "sender": email.sender,
                    "date": email.date,
                    "body_preview": email.body[:MAX_PREVIEW_LENGTH] + "..." if len(email.body) > MAX_PREVIEW_LENGTH else email.body,
                    "has_attachments": email.has_attachments,
                    "category": "Uncategorized",
                    "priority": "Medium",
                    "summary": "No summary available",
                    "action_required": False
                }
                
                # Add LLM analysis if available
                if i < len(summaries) and "error" not in summaries[i]:
                    summary = summaries[i]
                    email_data["category"] = summary.get("category", "Uncategorized")
                    email_data["priority"] = summary.get("priority", "Medium")
                    email_data["summary"] = summary.get("summary", "No summary available")
                    email_data["action_required"] = summary.get("action_required", False)
                    
                    # Update statistics
                    categories[email_data["category"]] = categories.get(email_data["category"], 0) + 1
                    priorities[email_data["priority"]] = priorities.get(email_data["priority"], 0) + 1
                    if email_data["action_required"]:
                        action_required_count += 1
                
                email_summaries.append(email_data)
            
            # Create summary report structure
            summary_report = {
                "total_emails": len(emails),
                "action_required_count": action_required_count,
                "categories": categories,
                "priorities": priorities,
                "emails": email_summaries
            }
            
            # Return structured JSON data
            return [TextContent(type="text", text=json.dumps(summary_report, indent=2))]
            
        except Exception as e:
            logger.error(f"Error in mail_summarize_emails: {e}")
            return [TextContent(type="text", text=f"Error summarizing emails: {str(e)}")]
    
    elif name == "mail_get_email_stats":
        try:
            if not await gmail_connector.connect():
                return [TextContent(type="text", text="Failed to connect to Gmail")]
            
            # Get basic inbox stats
            status, messages = gmail_connector.mail.search(None, 'ALL')
            total_count = len(messages[0].split()) if status == 'OK' and messages[0] else 0
            
            status, unread = gmail_connector.mail.search(None, 'UNSEEN')
            unread_count = len(unread[0].split()) if status == 'OK' and unread[0] else 0
            
            # Get recent emails for date range
            emails = await gmail_connector.fetch_last_emails(30)
            
            result = f"Gmail Inbox Statistics:\n\n"
            result += f"Total messages: {total_count}\n"
            result += f"Unread messages: {unread_count}\n"
            result += f"Read messages: {total_count - unread_count}\n\n"
            
            if emails:
                result += f"Recent activity (last 30 emails):\n"
                result += f"Date range: {emails[-1].date} to {emails[0].date}\n"
                result += f"Messages with attachments: {sum(1 for e in emails if e.has_attachments)}\n"
            
            return [TextContent(type="text", text=result)]
            
        except Exception as e:
            logger.error(f"Error in mail_get_email_stats: {e}")
            return [TextContent(type="text", text=f"Error getting email stats: {str(e)}")]
    
    elif name == "mail_find_emails_by_date":
        target_date = arguments.get("target_date")
        date_range = arguments.get("date_range", "on")
        
        if not target_date:
            return [TextContent(type="text", text="target_date parameter is required")]
        
        try:
            emails = await gmail_connector.fetch_emails_by_date(target_date, date_range)
            
            if not emails:
                range_text = {
                    "on": f"on {target_date}",
                    "since": f"since {target_date}",
                    "before": f"before {target_date}"
                }.get(date_range, f"for {target_date}")
                
                return [TextContent(type="text", text=f"No emails found {range_text}")]
            
            # Format emails for display
            range_text = {
                "on": f"on {target_date}",
                "since": f"since {target_date}",
                "before": f"before {target_date}"
            }.get(date_range, f"for {target_date}")
            
            result = f"Found {len(emails)} emails {range_text}:\\n\\n"
            
            for i, email in enumerate(emails, 1):
                result += f"{i}. Subject: {email.subject}\\n"
                result += f"   From: {email.sender}\\n"
                result += f"   Date: {email.date}\\n"
                result += f"   Attachments: {'Yes' if email.has_attachments else 'No'}\\n"
                
                # Add date analysis if available
                if email.date_info:
                    date_info = email.date_info
                    result += f"   Age: {date_info.get('age_days', 'unknown')} days\\n"
                    result += f"   Time Period: {date_info.get('time_period', 'unknown')}\\n"
                    if date_info.get('is_recent'):
                        result += f"   📍 Recent (< 24 hours)\\n"
                    if date_info.get('is_today'):
                        result += f"   📅 Today\\n"
                    if date_info.get('is_weekend'):
                        result += f"   🏖️ Weekend\\n"
                
                result += f"   Body Preview: {email.body[:MAX_PREVIEW_LENGTH]}{'...' if len(email.body) > MAX_PREVIEW_LENGTH else ''}\\n\\n"
            
            return [TextContent(type="text", text=result)]
            
        except Exception as e:
            logger.error(f"Error in mail_find_emails_by_date: {e}")
            return [TextContent(type="text", text=f"Error finding emails by date: {str(e)}")]
    
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

def _build_email_data(email: EmailMessage, summaries: List[Dict[str, Any]], index: int) -> Dict[str, Any]:
    """Build email data dictionary with LLM analysis if available"""
    email_data = {
        "uid": email.uid,
        "subject": email.subject,
        "sender": email.sender,
        "date": email.date,
        "body_preview": email.body[:MAX_PREVIEW_LENGTH] + "..." if len(email.body) > MAX_PREVIEW_LENGTH else email.body,
        "has_attachments": email.has_attachments,
        "category": "Uncategorized",
        "priority": "Medium",
        "summary": "No summary available",
        "action_required": False,
        "time_urgency": "normal"
    }
    
    # Add date analysis if available
    if email.date_info:
        date_info = email.date_info
        email_data.update({
            "date_analysis": {
                "is_recent": date_info.get("is_recent", False),
                "is_today": date_info.get("is_today", False),
                "is_weekend": date_info.get("is_weekend", False),
                "time_period": date_info.get("time_period", "unknown"),
                "age_hours": date_info.get("age_hours", None),
                "age_days": date_info.get("age_days", None),
                "timezone": date_info.get("timezone", None)
            }
        })
    
    # Add LLM analysis if available
    if index < len(summaries) and "error" not in summaries[index]:
        summary = summaries[index]
        email_data.update({
            "category": summary.get("category", "Uncategorized"),
            "priority": summary.get("priority", "Medium"),
            "summary": summary.get("summary", "No summary available"),
            "action_required": summary.get("action_required", False),
            "time_urgency": summary.get("time_urgency", "normal")
        })
    
    return email_data

async def main():
    """Main server function"""
    logger.info("Starting Email MCP Server...")
    
    # Test Gmail connection on startup
    if await gmail_connector.connect():
        logger.info("Gmail connection successful")
        gmail_connector.disconnect()
    else:
        logger.warning("Gmail connection failed - tools will attempt to reconnect when used")
    
    # Start MCP server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())