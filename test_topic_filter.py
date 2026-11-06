"""The topic category filter lives in hub.html and does not call the network."""
import json
import subprocess
import unittest
from pathlib import Path

NAMES = ['全部', '美食', '旅行', '科技', '科技科普', '科技产品', 'Tech Notes']


def filter_categories(names, query):
    html = Path(__file__).resolve().parent.joinpath('hub.html').read_text()
    start = html.index('function filterTopicCategories')
    end = html.index('/* end topic-category-filter */')
    script = html[start:end] + (
        '\nprocess.stdout.write(JSON.stringify({matches:filterTopicCategories(%s,%s),'
        'choice:topicCategoryFromQuery(%s,%s)}))'
    ) % (json.dumps(names, ensure_ascii=False), json.dumps(query, ensure_ascii=False),
         json.dumps(names, ensure_ascii=False), json.dumps(query, ensure_ascii=False))
    completed = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


class TopicCategoryFilterTests(unittest.TestCase):
    def test_substring_keeps_chinese_names_and_ignores_case(self):
        self.assertEqual(filter_categories(NAMES, '科技')['matches'], ['科技', '科技科普', '科技产品'])
        self.assertEqual(filter_categories(NAMES, 'TECH')['matches'], ['Tech Notes'])
        self.assertEqual(filter_categories(NAMES, '   ')['matches'], NAMES)
        self.assertEqual(filter_categories(NAMES, '')['matches'], NAMES)

    def test_enter_uses_exact_name_or_the_only_match(self):
        self.assertEqual(filter_categories(NAMES, '科技')['choice'], '科技')
        self.assertEqual(filter_categories(NAMES, '科普')['choice'], '科技科普')
        self.assertEqual(filter_categories(NAMES, 'tech notes')['choice'], 'Tech Notes')
        self.assertIsNone(filter_categories(NAMES, '科')['choice'])
        self.assertIsNone(filter_categories(NAMES, '没有')['choice'])
        self.assertIsNone(filter_categories(NAMES, '  ')['choice'])
