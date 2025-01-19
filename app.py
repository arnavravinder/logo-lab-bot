import os
from flask import Flask, request  # <-- Make sure 'request' is imported here
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.dialects.postgresql import UUID
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import uuid

load_dotenv()
app_flask = Flask(__name__)
slack_app = App(token=os.environ['SLACK_BOT_TOKEN'], signing_secret=os.environ['SLACK_SIGNING_SECRET'])
handler = SlackRequestHandler(slack_app)

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slack_id = Column(String, unique=True, nullable=False)
    username = Column(String, nullable=False)
    is_moderator = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Submission(Base):
    __tablename__ = 'submissions'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'))
    image_url = Column(String, nullable=False)
    description = Column(String, nullable=False)
    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    thread_ts = Column(String, nullable=True)

class Vote(Base):
    __tablename__ = 'votes'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey('users.id'))
    submission_id = Column(UUID(as_uuid=True), ForeignKey('submissions.id'))
    voted_at = Column(DateTime, default=datetime.utcnow)

engine = create_engine(os.environ['DATABASE_URL'])
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()
scheduler = BackgroundScheduler()
scheduler.start()

LOGOLAB_CHANNEL_ID = os.environ['LOGOLAB_CHANNEL_ID']
LOGO_REVIEWS_CHANNEL_ID = os.environ['LOGO_REVIEWS_CHANNEL_ID']
VOTING_DURATION_DAYS = int(os.environ.get('VOTING_DURATION_DAYS', 30))

MAIN_ADMIN_ID = "U078XLAFNMQ"

def ensure_main_admin(user):
    if user.slack_id == MAIN_ADMIN_ID and not user.is_moderator:
        user.is_moderator = True
        session.commit()

@slack_app.command("/upload")
def handle_upload(ack, body, respond):
    ack()
    user_id = body['user_id']
    text = body.get('text', '')
    if not text:
        respond("Provide a description and image URL: /upload <description> <image_url>")
        return
    parts = text.split()
    if len(parts) < 2:
        respond("Provide both description and image URL: /upload <description> <image_url>")
        return
    description = ' '.join(parts[:-1])
    image_url = parts[-1]
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user:
        user = User(slack_id=user_id, username=body['user_name'])
        session.add(user)
        session.commit()
    ensure_main_admin(user)
    submission = Submission(user_id=user.id, image_url=image_url, description=description)
    session.add(submission)
    session.commit()
    slack_app.client.chat_postMessage(
        channel=LOGO_REVIEWS_CHANNEL_ID,
        text=f"New logo submission from <@{user_id}>",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description:*\n{description}\n\n*Submission ID:*\n{submission.id}"
                },
                "accessory": {
                    "type": "image",
                    "image_url": image_url,
                    "alt_text": "Logo submission"
                }
            }
        ]
    )
    respond("Logo submitted for review.")

@slack_app.command("/approve")
def handle_approve(ack, body, respond):
    ack()
    user_id = body['user_id']
    text = body.get('text', '').strip()
    approver = session.query(User).filter_by(slack_id=user_id).first()
    if not approver:
        approver = User(slack_id=user_id, username=body['user_name'])
        session.add(approver)
        session.commit()
    ensure_main_admin(approver)
    if not text:
        respond("Provide a submission ID: /approve <submission_id>")
        return
    if not approver.is_moderator:
        respond("No permission to approve submissions.")
        return
    submission = session.query(Submission).filter_by(id=text, is_approved=False).first()
    if not submission:
        respond("Submission not found or already approved.")
        return
    submission.is_approved = True
    session.commit()
    poster = session.query(User).filter_by(id=submission.user_id).first()
    slack_msg = slack_app.client.chat_postMessage(
        channel=LOGOLAB_CHANNEL_ID,
        text=f"Approved logo by <@{poster.slack_id}>",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description:*\n{submission.description}\n\n*Submission ID:*\n{submission.id}"
                },
                "accessory": {
                    "type": "image",
                    "image_url": submission.image_url,
                    "alt_text": "Approved logo"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Vote"
                        },
                        "action_id": "vote",
                        "value": str(submission.id)
                    }
                ]
            }
        ]
    )
    submission.thread_ts = slack_msg['ts']
    session.commit()
    respond(f"Submission approved and posted to #logo-lab by <@{poster.slack_id}>.")

@slack_app.command("/make_mod")
def handle_make_mod(ack, body, respond):
    ack()
    user_id = body['user_id']
    commander = session.query(User).filter_by(slack_id=user_id).first()
    if not commander:
        commander = User(slack_id=user_id, username=body['user_name'])
        session.add(commander)
        session.commit()
    ensure_main_admin(commander)
    if not commander.is_moderator:
        respond("No permission to add moderators.")
        return
    text = body.get('text', '').strip()
    if not text:
        respond("Use: /make_mod <SlackUserID>")
        return
    target_id = text
    target_user = session.query(User).filter_by(slack_id=target_id).first()
    if not target_user:
        target_user = User(slack_id=target_id, username=target_id)
        session.add(target_user)
        session.commit()
    target_user.is_moderator = True
    session.commit()
    respond(f"User <@{target_id}> is now a moderator.")

@slack_app.command("/close_voting")
def handle_close_voting(ack, body, respond):
    ack()
    user_id = body['user_id']
    closer = session.query(User).filter_by(slack_id=user_id).first()
    if not closer:
        closer = User(slack_id=user_id, username=body['user_name'])
        session.add(closer)
        session.commit()
    ensure_main_admin(closer)
    if not closer.is_moderator:
        respond("No permission to close voting.")
        return
    submissions = session.query(Submission).filter_by(is_approved=True).all()
    if not submissions:
        respond("No approved submissions found.")
        return
    vote_counts = {}
    for s in submissions:
        count = session.query(Vote).filter_by(submission_id=s.id).count()
        vote_counts[s.id] = count
    if not vote_counts:
        respond("No votes cast.")
        return
    winner_id = max(vote_counts, key=vote_counts.get)
    winner = session.query(Submission).filter_by(id=winner_id).first()
    winning_user = session.query(User).filter_by(id=winner.user_id).first()
    slack_app.client.chat_postMessage(
        channel=LOGOLAB_CHANNEL_ID,
        text=f"🎉 <@{winning_user.slack_id}>'s logo won with {vote_counts[winner_id]} votes! 🎉"
    )
    session.query(Vote).delete()
    session.commit()
    respond("Voting closed and winner announced.")

@slack_app.action("vote")
def handle_vote(ack, body):
    ack()
    user_id = body['user']['id']
    submission_id = body['actions'][0]['value']
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user:
        user = User(slack_id=user_id, username=body['user'].get('username', 'UnknownUser'))
        session.add(user)
        session.commit()
    ensure_main_admin(user)
    existing_vote = session.query(Vote).filter_by(user_id=user.id).first()
    if existing_vote:
        slack_app.client.chat_postEphemeral(
            channel=body['channel']['id'],
            user=user_id,
            text="You have already voted."
        )
        return
    vote = Vote(user_id=user.id, submission_id=submission_id)
    session.add(vote)
    session.commit()
    total_votes = session.query(Vote).filter_by(submission_id=submission_id).count()
    thread_ts = None
    if "message" in body and body["message"].get("thread_ts"):
        thread_ts = body["message"]["thread_ts"]
    elif "message" in body and body["message"].get("ts"):
        thread_ts = body["message"]["ts"]
    slack_app.client.chat_postMessage(
        channel=body["channel"]["id"],
        text=f"Votes: {total_votes}",
        thread_ts=thread_ts
    )

def start_voting():
    submissions = session.query(Submission).filter_by(is_approved=True).all()
    for s in submissions:
        slack_app.client.chat_postMessage(
            channel=LOGOLAB_CHANNEL_ID,
            text="Vote for this logo:",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Description:*\n{s.description}\n\n*Submission ID:*\n{s.id}"
                    },
                    "accessory": {
                        "type": "image",
                        "image_url": s.image_url,
                        "alt_text": "Submission image"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Vote"
                            },
                            "action_id": "vote",
                            "value": str(s.id)
                        }
                    ]
                }
            ]
        )

scheduler.add_job(start_voting, 'interval', days=VOTING_DURATION_DAYS)

@app_flask.route("/slack/events", methods=["POST"])
def slack_events():
    # use the globally imported `request` from Flask
    return handler.handle(request)

if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
