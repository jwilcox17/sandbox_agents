import asyncio
import email
import imaplib
import json
import logging
import os
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_tz, mktime_tz
from typing import List, Dict, Any, Optional
from collections import defaultdict

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel

load_dotenv()



logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MAX_BODY_LENGTH = 500  # Reduced from 2000 to limit LLM input
MAX_TOTAL_EMAILS = 50  # Maximum total emails to prevent overload
DEFAULT_TIMEOUT = 30.0

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "jwilcox.email@gmail.com")
APP_PASSWORD = os.getenv("APP_PASSWORD", "ijexywomspgwpmvx")


class EmailMessage(BaseModel):
    uid: str
    subject: str
    sender: str
    date: str
    body: str
    has_attachments: bool = False
    is_read: bool = False
    date_info: Optional[Dict[str, Any]] = None

class EmailSummary(BaseModel):
    category: str
    priority: str
    summary: str
    action_required: bool = False
    time_urgency: str = "normal"
    sentiment: str = "neutral"
    topics: List[str] = []
    meeting_info: Optional[Dict[str, Any]] = None
    deadline_info: Optional[Dict[str, Any]] = None

class EmailGroup(BaseModel):
    category: str
    emails: List[Dict[str, Any]]
    common_summary: str
    total_count: int
    priority_distribution: Dict[str, int]

class GmailSummarizer:
    
    def __init__(self):
        self.mail = None
        self.connected = False
    
    async def connect(self) -> bool:
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
    
    @staticmethod
    def _parse_email_date(date_str: str) -> Dict[str, Any]:
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
            "time_period": "unknown"
        }
        
        try:
            date_tuple = parsedate_tz(date_str)
            if date_tuple:
                timestamp = mktime_tz(date_tuple)
                dt = datetime.fromtimestamp(timestamp)
                now = datetime.now()
                
                result["formatted_date"] = dt.strftime('%Y-%m-%d %H:%M:%S')
                result["timestamp"] = timestamp
                result["datetime_obj"] = dt
                
                if date_tuple[9] is not None:
                    tz_offset = date_tuple[9]
                    result["timezone"] = f"UTC{'+' if tz_offset >= 0 else ''}{tz_offset // 3600:02d}:{(abs(tz_offset) % 3600) // 60:02d}"
                
                age_delta = now - dt
                result["age_hours"] = age_delta.total_seconds() / 3600
                result["age_days"] = age_delta.days
                
                result["is_recent"] = age_delta.total_seconds() < 24 * 3600
                result["is_today"] = dt.date() == now.date()
                result["is_weekend"] = dt.weekday() >= 5
                
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
        if not msg.is_multipart():
            return False
        
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                return True
        return False
    
    async def fetch_unread_emails(self, count: int = 50) -> List[EmailMessage]:
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            status, messages = self.mail.search(None, 'UNSEEN')
            
            if status != 'OK':
                logger.error("Failed to search unread emails")
                return []
            
            message_uids = messages[0].split()
            
            if not message_uids:
                logger.info("No unread emails found")
                return []
            
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()
            
            for uid in recent_uids:
                try:
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    body = self.extract_email_body(msg)
                    has_attachments = self._has_attachments(msg)
                    
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
            
            logger.info(f"Successfully fetched {len(emails)} unread emails")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching unread emails: {e}")
            return []
    
    async def fetch_recent_emails(self, count: int = 30) -> List[EmailMessage]:
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            status, messages = self.mail.search(None, 'ALL')
            
            if status != 'OK':
                logger.error("Failed to search emails")
                return []
            
            message_uids = messages[0].split()
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()
            
            for uid in recent_uids:
                try:
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    body = self.extract_email_body(msg)
                    has_attachments = self._has_attachments(msg)
                    
                    email_obj = EmailMessage(
                        uid=uid.decode('utf-8'),
                        subject=subject,
                        sender=sender,
                        date=date_formatted,
                        body=body[:MAX_BODY_LENGTH],
                        has_attachments=has_attachments,
                        is_read=True,  # Assume read for recent emails analysis
                        date_info=date_info
                    )
                    
                    emails.append(email_obj)
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            logger.info(f"Successfully fetched {len(emails)} recent emails")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching recent emails: {e}")
            return []

class EmailProcessor:
    
    def __init__(self):
        pass
    
    def prepare_emails_for_analysis(self, emails: List[EmailMessage]) -> List[Dict[str, Any]]:
        email_data = []
        for email in emails:
            # Truncate body more aggressively and add preview indicator
            body_preview = email.body[:200] if email.body else ""
            if len(email.body) > 200:
                body_preview += "... [truncated]"
            
            email_info = {
                "uid": email.uid,
                "subject": email.subject,
                "sender": email.sender,
                "date": email.date,
                "body_preview": body_preview,  # Changed from 'body' to 'body_preview'
                "body_length": len(email.body) if email.body else 0,  # Add length info
                "has_attachments": email.has_attachments,
                "is_read": email.is_read
            }
            
            if email.date_info:
                date_info = email.date_info
                # Only include essential date info to reduce payload
                email_info.update({
                    "is_recent": date_info.get("is_recent", False),
                    "is_today": date_info.get("is_today", False),
                    "age_hours": round(date_info.get("age_hours", 0), 1) if date_info.get("age_hours") else None,
                    "time_period": date_info.get("time_period", "unknown")
                })
            
            email_data.append(email_info)
        
        return email_data
    
    def group_emails_by_sender(self, emails: List[Dict[str, Any]]) -> Dict[str, Any]:
        sender_groups = defaultdict(list)
        for email in emails:
            sender = email.get("sender", "Unknown")
            sender_groups[sender].append(email)
        
        return {
            "total_senders": len(sender_groups),
            "groups": dict(sender_groups),
            "group_stats": {sender: len(emails) for sender, emails in sender_groups.items()}
        }
    
    def create_date_timeline(self, emails: List[Dict[str, Any]]) -> Dict[str, Any]:
        timeline = defaultdict(list)
        
        for email in emails:
            email_date = email.get("date", "")
            if email_date:
                try:
                    date_part = email_date.split()[0] if " " in email_date else email_date[:10]
                    timeline[date_part].append({
                        "subject": email.get("subject", ""),
                        "sender": email.get("sender", ""),
                        "time": email_date,
                        "has_attachments": email.get("has_attachments", False)
                    })
                except:
                    timeline["unknown_date"].append(email)
        
        sorted_dates = sorted([d for d in timeline.keys() if d != "unknown_date"])
        
        return {
            "total_dates": len(timeline),
            "date_range": {"start": sorted_dates[0] if sorted_dates else None, "end": sorted_dates[-1] if sorted_dates else None},
            "timeline": dict(timeline),
            "daily_counts": {date: len(emails) for date, emails in timeline.items()}
        }
    
    

gmail_summarizer = GmailSummarizer()
email_processor = EmailProcessor()
server = Server("email-summarizer-server")

@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="comprehensive_email_analysis",
            description="Complete email analysis including unread emails, sender grouping, timeline, and structured data in one comprehensive report",
            inputSchema={
                "type": "object",
                "properties": {
                    "unread_count": {
                        "type": "integer",
                        "description": "Number of unread emails to analyze (default: 50, max: 100)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 100
                    },
                    "recent_count": {
                        "type": "integer",
                        "description": "Number of recent emails to include for comprehensive analysis (default: 100, max: 200)",
                        "default": 100,
                        "minimum": 20,
                        "maximum": 200
                    },
                    "include_timeline": {
                        "type": "boolean",
                        "description": "Include date-based timeline analysis (default: true)",
                        "default": True
                    },
                    "include_sender_groups": {
                        "type": "boolean",
                        "description": "Include sender grouping analysis (default: true)",
                        "default": True
                    },
                    "include_metadata": {
                        "type": "boolean",
                        "description": "Include detailed metadata for each email (default: true)",
                        "default": True
                    }
                }
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    
    if name == "comprehensive_email_analysis":
        unread_count = arguments.get("unread_count", 50)
        recent_count = arguments.get("recent_count", 100)
        include_timeline = arguments.get("include_timeline", True)
        include_sender_groups = arguments.get("include_sender_groups", True)
        include_metadata = arguments.get("include_metadata", True)
        
        try:
            unread_emails = await gmail_summarizer.fetch_unread_emails(unread_count)
            recent_emails = await gmail_summarizer.fetch_recent_emails(recent_count)
            
            if not unread_emails and not recent_emails:
                return [TextContent(type="text", text="No emails found or failed to connect to Gmail")]
            
            processed_unread = email_processor.prepare_emails_for_analysis(unread_emails) if unread_emails else []
            processed_recent = email_processor.prepare_emails_for_analysis(recent_emails) if recent_emails else []
            
            result = {
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": {
                    "total_unread": len(unread_emails),
                    "total_recent_analyzed": len(recent_emails),
                    "unread_with_attachments": len([e for e in processed_unread if e.get("has_attachments", False)]),
                    "recent_emails_today": len([e for e in processed_recent if e.get("is_today", False)]),
                    "unread_emails_today": len([e for e in processed_unread if e.get("is_today", False)])
                },
                "unread_emails": {
                    "count": len(processed_unread),
                    "emails": processed_unread if include_metadata else [{"subject": e.get("subject", ""), "sender": e.get("sender", ""), "date": e.get("date", ""), "body_length": e.get("body_length", 0)} for e in processed_unread],
                    "recent_unread_count": len([email for email in processed_unread if email.get("is_recent", False)]),
                    "today_unread_count": len([email for email in processed_unread if email.get("is_today", False)]),
                    "emails_with_attachments_count": len([email for email in processed_unread if email.get("has_attachments", False)])
                },
                "recent_emails": {
                    "count": len(processed_recent),
                    "emails": processed_recent if include_metadata else [{"subject": e.get("subject", ""), "sender": e.get("sender", ""), "date": e.get("date", ""), "body_length": e.get("body_length", 0)} for e in processed_recent]
                }
            }
            
            if include_sender_groups:
                # Only include stats, not full email groups to reduce payload
                unread_groups = email_processor.group_emails_by_sender(processed_unread) if processed_unread else {"total_senders": 0, "group_stats": {}}
                recent_groups = email_processor.group_emails_by_sender(processed_recent) if processed_recent else {"total_senders": 0, "group_stats": {}}
                combined_groups = email_processor.group_emails_by_sender(processed_unread + processed_recent) if (processed_unread or processed_recent) else {"total_senders": 0, "group_stats": {}}
                
                result["sender_analysis"] = {
                    "unread_sender_stats": {"total_senders": unread_groups["total_senders"], "group_stats": unread_groups["group_stats"]},
                    "recent_sender_stats": {"total_senders": recent_groups["total_senders"], "group_stats": recent_groups["group_stats"]},
                    "combined_sender_stats": {"total_senders": combined_groups["total_senders"], "group_stats": combined_groups["group_stats"]}
                }
            
            if include_timeline:
                # Only include summary stats, not full timeline details to reduce payload
                unread_timeline = email_processor.create_date_timeline(processed_unread) if processed_unread else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                recent_timeline = email_processor.create_date_timeline(processed_recent) if processed_recent else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                combined_timeline = email_processor.create_date_timeline(processed_unread + processed_recent) if (processed_unread or processed_recent) else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                
                result["timeline_analysis"] = {
                    "unread_timeline_summary": {"total_dates": unread_timeline["total_dates"], "date_range": unread_timeline["date_range"], "daily_counts": unread_timeline["daily_counts"]},
                    "recent_timeline_summary": {"total_dates": recent_timeline["total_dates"], "date_range": recent_timeline["date_range"], "daily_counts": recent_timeline["daily_counts"]},
                    "combined_timeline_summary": {"total_dates": combined_timeline["total_dates"], "date_range": combined_timeline["date_range"], "daily_counts": combined_timeline["daily_counts"]}
                }
            
            result["insights"] = {
                "unread_backlog": "High" if len(processed_unread) > 30 else "Medium" if len(processed_unread) > 10 else "Low",
                "attachment_ratio": round((len([e for e in processed_unread if e.get("has_attachments", False)]) / len(processed_unread) * 100) if processed_unread else 0, 1),
                "recent_activity": "High" if len([e for e in processed_recent if e.get("is_today", False)]) > 10 else "Medium" if len([e for e in processed_recent if e.get("is_today", False)]) > 5 else "Low",
                "recommendations": _generate_recommendations(processed_unread, processed_recent)
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        except Exception as e:
            logger.error(f"Error in comprehensive_email_analysis: {e}")
            return [TextContent(type="text", text=f"Error performing comprehensive analysis: {str(e)}")]
    
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

def _generate_recommendations(unread_emails: List[Dict[str, Any]], recent_emails: List[Dict[str, Any]]) -> List[str]:
    recommendations = []
    
    if len(unread_emails) > 50:
        recommendations.append("High unread count detected. Consider prioritizing or archiving emails.")
    elif len(unread_emails) > 20:
        recommendations.append("Moderate unread backlog. Review and process important emails first.")
    
    unread_today = len([e for e in unread_emails if e.get("is_today", False)])
    if unread_today > 10:
        recommendations.append(f"Multiple unread emails today ({unread_today}). Check for urgent items.")
    
    attachment_emails = len([e for e in unread_emails if e.get("has_attachments", False)])
    if attachment_emails > 5:
        recommendations.append(f"Several unread emails with attachments ({attachment_emails}). Review for important documents.")
    
    all_emails = unread_emails + recent_emails
    senders = {}
    for email in all_emails:
        sender = email.get("sender", "Unknown")
        senders[sender] = senders.get(sender, 0) + 1
    
    frequent_senders = [(sender, count) for sender, count in senders.items() if count > 5]
    if frequent_senders:
        top_sender = max(frequent_senders, key=lambda x: x[1])
        recommendations.append(f"Frequent sender detected: {top_sender[0]} ({top_sender[1]} emails). Consider filtering or priority rules.")
    
    if not recommendations:
        recommendations.append("Email management looks healthy. Continue current practices.")
    
    return recommendations

async def main():
    logger.info("Starting Email Summarizer Server...")
    
    if await gmail_summarizer.connect():
        logger.info("Gmail connection successful")
        gmail_summarizer.disconnect()
    else:
        logger.warning("Gmail connection failed - tools will attempt to reconnect when used")
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())