from plastron.models.newspaper import Issue

base_uri = 'http://example.com/xyz'


def test_issue_invalid_with_no_fields():
    issue = Issue()
    assert not issue.is_valid


def test_issue_valid_with_only_required_fields():
    issue = Issue()

    # Only provide required fields
    issue.identifier = 'test_issue'
    issue.title = 'Test Issue'
    issue.date = '1970-01-01'
    issue.volume = '1'
    issue.issue = '1'
    issue.edition = '1'

    assert issue.is_valid
