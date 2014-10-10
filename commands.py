from jeev.message import Attachment
import module


color_to_hex = {
    'green': '#14892c',
    'yellow': '#ffd351',
    'brown': '#815b3a',
    'warm-red': '#d04437',
    'blue-gray': '#4a6785',
    'medium-gray': '#cccccc',
}


@module.match('(?P<issue_key>[A-Za-z]+\-\d+)')
@module.async(module.STOP)
def jira_issue(message, issue_key):
    issue = module.g.index.get_by_key(issue_key.upper(), module.g.index.data_key)
    if issue is not None:
        fields = issue['fields']
        link = '%s/browse/%s' % (module.opts['server'], issue['key'])
        a = Attachment(link) \
            .icon(fields['issuetype']['iconUrl']) \
            .color(color_to_hex.get(fields['status']['statusCategory']['colorName'], 'good')) \
            .name("jiracache") \
            .field("Summary", fields['summary']) \
            .field("Status", fields['status']['name'], True) \
            .field("Assignee", fields['assignee'] and fields['assignee']['displayName'] or 'Unassigned', True) \

        message.reply_with_attachment(a)


@module.loaded
def init():
    module.g.syncing = False


@module.respond('resync jiracache cache')
@module.async()
def resync(message):
    if module.g.syncing:
        message.reply_to_user("I'm already syncing!")
        return

    message.reply_to_user('Starting jiracache cache sync.')
    module.g.syncing = True
    try:
        for i in resync_iter():
            message.reply_to_user("Synced %i issues so far." % i)

    except Exception, e:
        message.reply_to_user("An error happened! %r" % e)

    else:
        message.reply_to_user('Done syncing!')

    finally:
        module.g.syncing = False


def resync_iter():
    jira = module.g.client
    jira_index = module.g.index
    max_result = 250
    i = 0
    jira_index.flush(everything=True)
    module.g.connections.trigger_update()

    while True:
        issues = jira.search_issues('', maxResults=max_result, startAt=i, expand="renderedFields")

        if not issues:
            break

        for issue in issues:
            jira_index.index(issue)

        module.g.connections.trigger_update()
        i += max_result
        yield i
