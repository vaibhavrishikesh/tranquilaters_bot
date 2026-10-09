# TranquilWaters Bot

Telegram assistant for TranquilWaters/Hermes staff workflows.

Current scope:

- Guest reply drafting
- Complaint/message summarization
- WhatsApp/message wording
- Read-only PMS reports through configured Zimmerstack PMS credentials

PMS write actions such as bookings, payments, check-in, checkout, and room updates stay outside this bot.


## Google Docs integration

The bot can save read-only PMS summaries to Google Docs with `/doc_report`.

Setup:

1. Enable Google Docs API and Google Drive API in Google Cloud.
2. Create a service account and download its JSON key to the VPS.
3. Share the destination Google Drive folder or Google Doc with the service account email.
4. Set `GOOGLE_APPLICATION_CREDENTIALS` to the JSON key path.
5. Set either `GOOGLE_DOCS_FOLDER_ID` to create dated report docs in that folder, or `GOOGLE_DOCS_REPORT_ID` to append to one existing document.

Commands:

- `/doc_status` checks whether the integration is configured.
- `/doc_report` appends the current PMS summary to Google Docs.
