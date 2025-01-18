import os
from flask import Flask, request, abort
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from dotenv import load_dotenv
from models import Base, User, Submission, Vote
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import uuid

load_dotenv()
app_flask = Flask(__name__)
slack_app = App(token=os.environ['SLACK_BOT_TOKEN'], signing_secret=os.environ['SLACK_SIGNING_SECRET'])
handler = SlackRequestHandler(slack_app)
engine = create_engine(os.environ['DATABASE_URL'])
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()
scheduler = BackgroundScheduler()
scheduler.start()
LOGOLAB_CHANNEL_ID = os.environ['LOGOLAB_CHANNEL_ID']
LOGO_REVIEWS_CHANNEL_ID = os.environ['LOGO_REVIEWS_CHANNEL_ID']
VOTING_DURATION_DAYS = int(os.environ.get('VOTING_DURATION_DAYS', 30))

@slack_app.command("/upload")
def handle_upload(ack, body, respond):
    ack()
    user_id = body['user_id']
    text = body.get('text', '')
    channel_id = body['channel_id']
    if not text:
        respond("Please provide a description for your logo. Usage: /upload <description> <image_url>")
        return
    parts = text.split()
    if len(parts) < 2:
        respond("Please provide both a description and an image URL. Usage: /upload <description> <image_url>")
        return
    description = ' '.join(parts[:-1])
    image_url = parts[-1]
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user:
        user = User(slack_id=user_id, username=body['user_name'])
        session.add(user)
        session.commit()
    submission = Submission(user_id=user.id, image_url=image_url, description=description)
    session.add(submission)
    session.commit()
    slack_app.client.chat_postMessage(
        channel=LOGO_REVIEWS_CHANNEL_ID,
        text=f"New logo submission from <@{user_id}>",
        attachments=[{
            "image_url": image_url,
            "text": description,
            "footer": f"Submission ID: {submission.id}"
        }]
    )
    respond("Your logo has been submitted for review.")

@slack_app.command("/approve")
def handle_approve(ack, body, respond):
    ack()
    user_id = body['user_id']
    text = body.get('text', '').strip()
    if not text:
        respond("Please provide a submission ID to approve. Usage: /approve <submission_id>")
        return
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user or not user.is_moderator:
        respond("You do not have permission to approve submissions.")
        return
    try:
        submission_id = uuid.UUID(text)
    except:
        respond("Invalid submission ID.")
        return
    submission = session.query(Submission).filter_by(id=submission_id, is_approved=False).first()
    if not submission:
        respond("Submission not found or already approved.")
        return
    submission.is_approved = True
    session.commit()
    slack_app.client.chat_postMessage(
        channel=LOGOLAB_CHANNEL_ID,
        text=f"New approved logo by <@{submission.user_id}>",
        attachments=[{
            "image_url": submission.image_url,
            "text": submission.description,
            "footer": f"Submission ID: {submission.id}"
        }]
    )
    respond("Submission approved and posted to #logo-lab.")

@slack_app.action("vote")
def handle_vote(ack, body, respond):
    ack()
    user_id = body['user']['id']
    submission_id = body['actions'][0]['value']
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user:
        user = User(slack_id=user_id, username=body['user']['username'])
        session.add(user)
        session.commit()
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
    slack_app.client.chat_postMessage(
        channel=body['channel']['id'],
        text=f"<@{user_id}> has voted.",
        thread_ts=body['message']['thread_ts']
    )

@slack_app.command("/close_voting")
def handle_close_voting(ack, body, respond):
    ack()
    user_id = body['user_id']
    user = session.query(User).filter_by(slack_id=user_id).first()
    if not user or not user.is_moderator:
        respond("You do not have permission to close voting.")
        return
    submissions = session.query(Submission).filter_by(is_approved=True).all()
    vote_counts = {}
    for submission in submissions:
        count = session.query(Vote).filter_by(submission_id=submission.id).count()
        vote_counts[submission.id] = count
    if not vote_counts:
        respond("No votes have been cast.")
        return
    winner_id = max(vote_counts, key=vote_counts.get)
    winner = session.query(Submission).filter_by(id=winner_id).first()
    slack_app.client.chat_postMessage(
        channel=LOGOLAB_CHANNEL_ID,
        text=f"🎉 Congratulations to <@{winner.user_id}>! Your logo has been selected as the official Hack Club logo! 🎉"
    )
    session.query(Vote).delete()
    session.commit()
    respond("Voting closed and winner announced.")

def start_voting():
    submissions = session.query(Submission).filter_by(is_approved=True).all()
    for submission in submissions:
        slack_app.client.chat_postMessage(
            channel=LOGOLAB_CHANNEL_ID,
            text=f"Vote for this logo:",
            attachments=[{
                "image_url": submission.image_url,
                "text": submission.description,
                "actions": [{
                    "name": "vote",
                    "text": "Vote",
                    "type": "button",
                    "value": str(submission.id)
                }]
            }]
        )

scheduler.add_job(start_voting, 'interval', days=VOTING_DURATION_DAYS)

@app_flask.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

if __name__ == "__main__":
    app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))