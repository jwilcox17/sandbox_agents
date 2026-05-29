import asyncio
import email
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_tz, mktime_tz
from typing import List, Dict, Any
from collections import Counter

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

DEFAULT_TIMEOUT = 30.0

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

class EmailStats(BaseModel):
    total_emails: int
    unread_emails: int
    read_emails: int
    emails_with_attachments: int
    sender_distribution: Dict[str, int]
    domain_distribution: Dict[str, int]
    date_distribution: Dict[str, int]
    time_distribution: Dict[str, int]
    email_types: Dict[str, int]
    keyword_matches: Dict[str, int]
    recommendations: List[str]

class KeywordMatch(BaseModel):
    keyword: str
    email_uid: str
    subject: str
    sender: str
    date: str
    match_location: str  # "subject", "body", "both"
    match_count: int
    context: str  # snippet of text around the match

class GmailMonitor:
    
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
    
    def extract_sender_domain(self, sender: str) -> str:
        try:
            if '<' in sender and '>' in sender:
                email_part = sender.split('<')[1].split('>')[0]
            else:
                email_part = sender
            
            if '@' in email_part:
                return email_part.split('@')[1].lower()
            return "unknown"
        except:
            return "unknown"
    
    def search_keywords_in_email(self, keywords: List[str], subject: str, body: str) -> Dict[str, Any]:
        matches = {}
        
        for keyword in keywords:
            keyword_lower = keyword.lower()
            subject_lower = subject.lower()
            body_lower = body.lower()
            
            subject_matches = len(re.findall(re.escape(keyword_lower), subject_lower))
            body_matches = len(re.findall(re.escape(keyword_lower), body_lower))
            total_matches = subject_matches + body_matches
            
            if total_matches > 0:
                match_location = []
                if subject_matches > 0:
                    match_location.append("subject")
                if body_matches > 0:
                    match_location.append("body")
                
                context = ""
                if body_matches > 0:
                    pattern = re.compile(f".{{0,50}}{re.escape(keyword_lower)}.{{0,50}}", re.IGNORECASE)
                    match = pattern.search(body)
                    if match:
                        context = match.group(0).strip()
                
                matches[keyword] = {
                    "total_count": total_matches,
                    "subject_count": subject_matches,
                    "body_count": body_matches,
                    "location": ", ".join(match_location),
                    "context": context
                }
        
        return matches
    
    def classify_email_type(self, subject: str, sender: str, body: str = "") -> str:
        subject_lower = subject.lower()
        sender_lower = sender.lower()
        
        marketing_keywords = ['sale', 'discount', 'offer', 'promotion', 'deal', 'newsletter', 
                             'subscribe', 'unsubscribe', 'marketing', 'campaign']
        notification_keywords = ['notification', 'alert', 'reminder', 'update', 'report',
                               'status', 'confirmation', 'receipt', 'invoice']
        support_keywords = ['support', 'help', 'service', 'ticket', 'issue', 'problem',
                           'assistance', 'customer']
        work_keywords = ['meeting', 'project', 'deadline', 'report', 'proposal', 'contract',
                        'business', 'work', 'office', 'team']
        social_keywords = ['hi', 'hello', 'thank you', 'thanks', 'regards', 'personal',
                          'family', 'friend']
        
        if any(domain in sender_lower for domain in ['noreply', 'no-reply', 'donotreply']):
            return "automated"
        
        if any(keyword in subject_lower for keyword in marketing_keywords):
            return "marketing"
        elif any(keyword in subject_lower for keyword in notification_keywords):
            return "notification"
        elif any(keyword in subject_lower for keyword in support_keywords):
            return "support"
        elif any(keyword in subject_lower for keyword in work_keywords):
            return "work"
        elif any(keyword in subject_lower for keyword in social_keywords):
            return "personal"
        else:
            return "other"
    
    async def fetch_email_statistics(self, count: int = 100, keywords: List[str] = None) -> EmailStats:
        if not self.connected:
            if not await self.connect():
                return EmailStats(
                    total_emails=0, unread_emails=0, read_emails=0,
                    emails_with_attachments=0, sender_distribution={},
                    domain_distribution={}, date_distribution={},
                    time_distribution={}, email_types={},
                    keyword_matches={}, recommendations=["Failed to connect to Gmail"]
                )
        
        try:
            status, messages = self.mail.search(None, 'ALL')
            total_count = len(messages[0].split()) if status == 'OK' and messages[0] else 0
            
            status, unread = self.mail.search(None, 'UNSEEN')
            unread_count = len(unread[0].split()) if status == 'OK' and unread[0] else 0
            
            sender_counter = Counter()
            domain_counter = Counter()
            date_counter = Counter()
            time_counter = Counter()
            type_counter = Counter()
            attachment_count = 0
            keyword_counter = Counter()
            keyword_matches = []
            if keywords is None:
                keywords = []
            
            status, messages = self.mail.search(None, 'ALL')
            if status != 'OK':
                raise Exception("Failed to search emails")
            
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
                    
                    sender_counter[sender] += 1
                    domain = self.extract_sender_domain(sender)
                    domain_counter[domain] += 1
                    
                    try:
                        date_tuple = parsedate_tz(date_str)
                        if date_tuple:
                            dt = datetime.fromtimestamp(mktime_tz(date_tuple))
                            date_counter[dt.strftime('%Y-%m-%d')] += 1
                            hour = dt.hour
                            if 5 <= hour < 12:
                                time_counter['morning'] += 1
                            elif 12 <= hour < 17:
                                time_counter['afternoon'] += 1
                            elif 17 <= hour < 21:
                                time_counter['evening'] += 1
                            else:
                                time_counter['night'] += 1
                    except:
                        pass
                    
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_disposition() == 'attachment':
                                attachment_count += 1
                                break
                    
                    body = ""
                    try:
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode('utf-8', errors='ignore').strip()
                                        break
                        else:
                            if msg.get_content_type() == "text/plain":
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    body = payload.decode('utf-8', errors='ignore').strip()
                    except Exception as e:
                        logger.debug(f"Error extracting body for keyword search: {e}")
                        body = ""
                    
                    if keywords:
                        keyword_results = self.search_keywords_in_email(keywords, subject, body)
                        for keyword, result in keyword_results.items():
                            keyword_counter[keyword] += result["total_count"]
                            keyword_matches.append({
                                "keyword": keyword,
                                "email_uid": uid.decode('utf-8'),
                                "subject": subject,
                                "sender": sender,
                                "date": date_str,
                                "match_location": result["location"],
                                "match_count": result["total_count"],
                                "context": result["context"]
                            })
                    
                    email_type = self.classify_email_type(subject, sender, body)
                    type_counter[email_type] += 1
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            # Generate recommendations
            recommendations = self._generate_recommendations(
                total_count, unread_count, sender_counter, domain_counter, 
                type_counter, attachment_count, count, keyword_counter
            )
            
            return EmailStats(
                total_emails=total_count,
                unread_emails=unread_count,
                read_emails=total_count - unread_count,
                emails_with_attachments=attachment_count,
                sender_distribution=dict(sender_counter.most_common(10)),
                domain_distribution=dict(domain_counter.most_common(10)),
                date_distribution=dict(sorted(date_counter.items())[-7:]),
                time_distribution=dict(time_counter),
                email_types=dict(type_counter),
                keyword_matches=dict(keyword_counter),
                recommendations=recommendations
            )
            
        except Exception as e:
            logger.error(f"Error fetching email statistics: {e}")
            return EmailStats(
                total_emails=0, unread_emails=0, read_emails=0,
                emails_with_attachments=0, sender_distribution={},
                domain_distribution={}, date_distribution={},
                time_distribution={}, email_types={},
                keyword_matches={}, recommendations=[f"Error analyzing emails: {str(e)}"]
            )
    
    def _generate_recommendations(self, total_count: int, unread_count: int, 
                                sender_counter: Counter, domain_counter: Counter,
                                type_counter: Counter, attachment_count: int, 
                                analyzed_count: int, keyword_counter: Counter = None) -> List[str]:
        recommendations = []
        
        if unread_count > 50:
            recommendations.append(f"High unread count ({unread_count}). Consider processing or archiving old emails.")
        elif unread_count > 20:
            recommendations.append(f"Moderate unread count ({unread_count}). Review and prioritize important emails.")
        
        top_senders = sender_counter.most_common(3)
        if top_senders:
            top_sender, count = top_senders[0]
            if count > analyzed_count * 0.2:
                recommendations.append(f"High frequency sender detected: {top_sender} ({count} emails). Consider filtering or creating a rule.")
        
        marketing_domains = ['marketing', 'newsletter', 'noreply', 'no-reply']
        marketing_count = sum(count for domain, count in domain_counter.items() 
                            if any(keyword in domain.lower() for keyword in marketing_domains))
        
        if marketing_count > analyzed_count * 0.3:
            recommendations.append(f"High marketing email volume ({marketing_count}). Consider unsubscribing or filtering.")
        
        if type_counter.get('marketing', 0) > analyzed_count * 0.4:
            recommendations.append("High marketing email ratio. Review subscriptions and set up filters.")
        
        if type_counter.get('notification', 0) > analyzed_count * 0.3:
            recommendations.append("Many notification emails. Consider consolidating or reducing notification frequency.")
        
        if attachment_count > analyzed_count * 0.1:
            recommendations.append(f"Many emails with attachments ({attachment_count}). Review storage and download important files.")
        
        if keyword_counter:
            total_keyword_matches = sum(keyword_counter.values())
            if total_keyword_matches > 0:
                top_keywords = keyword_counter.most_common(3)
                recommendations.append(f"Keyword tracking found {total_keyword_matches} matches. Top keywords: {', '.join([f'{k}({c})' for k, c in top_keywords])}")
        
        if total_count > 5000:
            recommendations.append("Large inbox size. Consider archiving old emails or using folders for organization.")
        
        if not recommendations:
            recommendations.append("Inbox health looks good! Continue current email management practices.")
        
        return recommendations

gmail_monitor = GmailMonitor()
server = Server("email-monitor-server")

@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="comprehensive_email_monitoring",
            description="Complete email monitoring analysis including statistics, sender analysis, domain analysis, email classification, keyword tracking, and recommendations in one comprehensive report",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of emails to analyze (default: 200, max: 500)",
                        "default": 200,
                        "minimum": 50,
                        "maximum": 500
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of keywords to track in emails (e.g., ['meeting', 'urgent', 'deadline', 'sports'])",
                        "default": []
                    },
                    "top_senders_limit": {
                        "type": "integer",
                        "description": "Number of top senders to include in analysis (default: 15, max: 50)",
                        "default": 15,
                        "minimum": 5,
                        "maximum": 50
                    },
                    "include_productivity_score": {
                        "type": "boolean",
                        "description": "Include productivity score calculation based on email types (default: true)",
                        "default": True
                    },
                    "include_detailed_recommendations": {
                        "type": "boolean",
                        "description": "Include detailed recommendations and insights (default: true)",
                        "default": True
                    }
                }
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    
    if name == "comprehensive_email_monitoring":
        count = arguments.get("count", 200)
        keywords = arguments.get("keywords", [])
        top_senders_limit = arguments.get("top_senders_limit", 15)
        include_productivity_score = arguments.get("include_productivity_score", True)
        include_detailed_recommendations = arguments.get("include_detailed_recommendations", True)
        
        try:
            stats = await gmail_monitor.fetch_email_statistics(count, keywords)
            
            # Calculate type percentages for productivity score
            type_percentages = {}
            for email_type, type_count in stats.email_types.items():
                type_percentages[email_type] = round((type_count / count * 100) if count > 0 else 0, 1)
            
            # Categorize domains
            business_domains = []
            personal_domains = []
            service_domains = []
            
            common_personal = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com']
            common_service = ['noreply', 'no-reply', 'donotreply', 'support', 'service']
            
            for domain, count_val in stats.domain_distribution.items():
                if domain in common_personal:
                    personal_domains.append({"domain": domain, "count": count_val})
                elif any(service in domain.lower() for service in common_service):
                    service_domains.append({"domain": domain, "count": count_val})
                else:
                    business_domains.append({"domain": domain, "count": count_val})
            
            # Generate comprehensive insights
            comprehensive_insights = []
            if type_percentages.get('marketing', 0) > 30:
                comprehensive_insights.append("High marketing email volume - consider subscription management")
            if type_percentages.get('work', 0) > 50:
                comprehensive_insights.append("Work-heavy inbox - ensure proper prioritization")
            if type_percentages.get('automated', 0) > 40:
                comprehensive_insights.append("Many automated emails - review notification settings")
            if type_percentages.get('personal', 0) < 10:
                comprehensive_insights.append("Low personal email ratio - inbox may be too business-focused")
            
            # Sender concentration analysis
            sender_concentration = {
                "top_3_percentage": sum(list(stats.sender_distribution.values())[:3]) / count * 100 if count > 0 else 0,
                "top_10_percentage": sum(list(stats.sender_distribution.values())[:10]) / count * 100 if count > 0 else 0
            }
            
            # Keyword analysis if keywords provided
            keyword_analysis = None
            if keywords:
                keyword_analysis = {
                    "tracked_keywords": keywords,
                    "keyword_matches": stats.keyword_matches,
                    "total_matches": sum(stats.keyword_matches.values()),
                    "match_rate_percentage": round((sum(stats.keyword_matches.values()) / count * 100) if count > 0 else 0, 2),
                    "keyword_insights": []
                }
                
                for keyword, match_count in stats.keyword_matches.items():
                    if match_count > 0:
                        match_rate = round((match_count / count * 100), 2)
                        keyword_analysis["keyword_insights"].append({
                            "keyword": keyword,
                            "matches": match_count,
                            "match_rate_percentage": match_rate,
                            "recommendation": f"Found {match_count} emails containing '{keyword}' ({match_rate}% of analyzed emails)"
                        })
                
                if not keyword_analysis["keyword_insights"]:
                    keyword_analysis["keyword_insights"].append({
                        "message": "No matches found for any of the tracked keywords in the analyzed emails"
                    })
            
            # Build comprehensive report
            comprehensive_report = {
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": {
                    "total_emails": stats.total_emails,
                    "unread_emails": stats.unread_emails,
                    "read_emails": stats.read_emails,
                    "read_percentage": round((stats.read_emails / stats.total_emails * 100) if stats.total_emails > 0 else 0, 1),
                    "emails_with_attachments": stats.emails_with_attachments,
                    "analyzed_emails": count,
                    "unique_senders": len(stats.sender_distribution),
                    "unique_domains": len(stats.domain_distribution)
                },
                "sender_analysis": {
                    "total_unique_senders": len(stats.sender_distribution),
                    "top_senders": dict(list(stats.sender_distribution.items())[:top_senders_limit]),
                    "sender_concentration": sender_concentration,
                    "full_distribution": stats.sender_distribution
                },
                "domain_analysis": {
                    "total_unique_domains": len(stats.domain_distribution),
                    "domain_distribution": stats.domain_distribution,
                    "categorized_domains": {
                        "business": business_domains,
                        "personal": personal_domains,
                        "service_automated": service_domains
                    },
                    "domain_diversity": round(len(stats.domain_distribution) / count if count > 0 else 0, 3)
                },
                "email_type_analysis": {
                    "email_type_counts": stats.email_types,
                    "email_type_percentages": type_percentages,
                    "type_insights": comprehensive_insights
                },
                "temporal_analysis": {
                    "date_distribution": stats.date_distribution,
                    "time_distribution": stats.time_distribution
                }
            }
            
            if include_productivity_score:
                comprehensive_report["productivity_analysis"] = _calculate_productivity_score(type_percentages)
            
            if keyword_analysis:
                comprehensive_report["keyword_analysis"] = keyword_analysis
            
            if include_detailed_recommendations:
                comprehensive_report["recommendations"] = stats.recommendations
                comprehensive_report["actionable_insights"] = _generate_actionable_insights(stats, type_percentages, sender_concentration)
            
            return [TextContent(type="text", text=json.dumps(comprehensive_report, indent=2))]
            
        except Exception as e:
            logger.error(f"Error in comprehensive_email_monitoring: {e}")
            return [TextContent(type="text", text=f"Error performing comprehensive monitoring: {str(e)}")]
    
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

def _generate_actionable_insights(stats, type_percentages: Dict[str, float], sender_concentration: Dict[str, float]) -> List[str]:
    insights = []
    
    # Unread analysis
    if stats.unread_emails > 100:
        insights.append("URGENT: Very high unread count. Consider bulk archiving or creating priority filters.")
    elif stats.unread_emails > 50:
        insights.append("HIGH: Significant unread backlog. Schedule dedicated email processing time.")
    
    # Sender concentration
    if sender_concentration["top_3_percentage"] > 40:
        insights.append("HIGH: Top 3 senders dominate inbox. Create specific folders or filters for frequent senders.")
    
    # Email type balance
    marketing_ratio = type_percentages.get('marketing', 0)
    work_ratio = type_percentages.get('work', 0)
    
    if marketing_ratio > 40:
        insights.append("HIGH: Marketing emails exceed 40%. Unsubscribe from unnecessary lists and set up filters.")
    if work_ratio > 70:
        insights.append("MEDIUM: Work emails dominate (>70%). Ensure work-life balance in email habits.")
    if work_ratio < 20 and marketing_ratio > 30:
        insights.append("MEDIUM: Low work-to-marketing ratio suggests need for better filtering.")
    
    # Productivity insights
    if type_percentages.get('notification', 0) > 25:
        insights.append("MEDIUM: High notification volume. Review and reduce automated email subscriptions.")
    
    # Domain insights
    if len(stats.domain_distribution) < 5:
        insights.append("LOW: Limited domain diversity. Consider expanding professional network or interests.")
    elif len(stats.domain_distribution) > 50:
        insights.append("MEDIUM: Very high domain diversity. Consider consolidating or organizing communications.")
    
    if not insights:
        insights.append("GOOD: Email patterns appear healthy and well-managed.")
    
    return insights

def _calculate_productivity_score(type_percentages: Dict[str, float]) -> Dict[str, Any]:
    work_weight = type_percentages.get('work', 0) * 0.3
    personal_weight = type_percentages.get('personal', 0) * 0.2
    marketing_penalty = type_percentages.get('marketing', 0) * -0.2
    notification_penalty = type_percentages.get('notification', 0) * -0.1
    
    raw_score = work_weight + personal_weight + marketing_penalty + notification_penalty + 50
    productivity_score = max(0, min(100, raw_score))
    
    if productivity_score >= 80:
        rating = "Excellent"
    elif productivity_score >= 60:
        rating = "Good"
    elif productivity_score >= 40:
        rating = "Fair"
    else:
        rating = "Needs Improvement"
    
    return {
        "score": round(productivity_score, 1),
        "rating": rating,
        "factors": {
            "work_focus": work_weight,
            "personal_balance": personal_weight,
            "marketing_overhead": marketing_penalty,
            "notification_overhead": notification_penalty
        }
    }

async def main():
    if not EMAIL_ADDRESS or not APP_PASSWORD:
        print("ERROR: EMAIL_ADDRESS and APP_PASSWORD environment variables must be set.", file=sys.stderr)
        sys.exit(1)
    logger.info("Starting Email Monitor Server...")

    if await gmail_monitor.connect():
        logger.info("Gmail connection successful")
        gmail_monitor.disconnect()
    else:
        logger.warning("Gmail connection failed - tools will attempt to reconnect when used")
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())