# import vertexai
# from vertexai.generative_models import GenerativeModel

# vertexai.init(
#     project="data-science-test-481612",
#     location="us-central1",
# )

# model = GenerativeModel("gemini-2.5-flash")
# response = model.generate_content("Say hello in one sentence.")
# print(response.text)

# import asyncio
# from google.cloud import logging as gcp_logging
# from datetime import datetime, timezone, timedelta

# client = gcp_logging.Client(project='omega-sorter-353009')
# cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
# cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

# log_filter = (
#     'resource.type=\"cloud_run_revision\" '
#     'severity>=\"WARNING\" '
#     f'timestamp>=\"{cutoff_str}\"'
# )

# entries = list(client.list_entries(filter_=log_filter, max_results=5))
# print(f'Total entries: {len(entries)}')
# for e in entries:
#     print(f'  payload_type={e.payload_type!r}')
#     print(f'  severity={e.severity}')
#     print(f'  payload={str(e.payload)[:100]}')
#     print()


# from google.cloud import logging as gcp_logging
# from google.cloud.logging_v2.entries import TextEntry, StructuredLogEntry
# from datetime import datetime, timezone, timedelta

# client = gcp_logging.Client(project='omega-sorter-353009')
# cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
# cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

# log_filter = (
#     'resource.type=\"cloud_run_revision\" '
#     'severity>=\"WARNING\" '
#     f'timestamp>=\"{cutoff_str}\"'
# )

# entries = list(client.list_entries(filter_=log_filter, max_results=10))
# print(f'Total entries: {len(entries)}')

# for e in entries:
#     entry_type = type(e).__name__
#     print(f'--- entry_type={entry_type}')
#     print(f'    payload={str(e.payload)[:150]}')
#     print(f'    http_request={str(e.http_request)[:150]}')
#     print(f'    severity={e.severity}')


# import google.cloud.logging_v2.entries as e
# print(dir(e))



# from google.cloud import logging as gcp_logging
# from datetime import datetime, timezone, timedelta

# client = gcp_logging.Client(project='omega-sorter-353009')
# cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
# cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

# log_filter = (
#     'resource.type=\"cloud_run_revision\" '
#     'severity>=\"WARNING\" '
#     f'timestamp>=\"{cutoff_str}\"'
# )

# entries = list(client.list_entries(filter_=log_filter, max_results=10))
# print(f'Total entries: {len(entries)}')
# for e in entries:
#     print(f'  class={type(e).__name__}, payload_type={type(e.payload).__name__}, payload={str(e.payload)[:100]}')


from google.cloud import logging as gcp_logging
from datetime import datetime, timezone, timedelta

client = gcp_logging.Client(project='omega-sorter-353009')
cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')

log_filter = (
    'resource.type=\"cloud_run_revision\" '
    'severity>=\"WARNING\" '
    f'timestamp>=\"{cutoff_str}\"'
)

entries = list(client.list_entries(filter_=log_filter, max_results=10))
parsed = []
for e in entries:
    message = ''
    entry_class = type(e).__name__
    if entry_class == 'TextEntry':
        message = e.payload or ''
    elif entry_class == 'LogEntry':
        http = e.http_request
        if http:
            status = http.get('status', '')
            method = http.get('requestMethod', '')
            url = http.get('requestUrl', '')
            if str(status).startswith(('4', '5')):
                message = f'HTTP {status} {method} {url}'
    if message and message.strip() not in ('', '{}', 'None'):
        parsed.append(message)

print(f'Parsed {len(parsed)} meaningful entries:')
for m in parsed:
    print(f'  -> {m[:120]}')
