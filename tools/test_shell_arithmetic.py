"""Arithmetic subscript quoting is distinct from standalone quote characters."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402


class ArithmeticTests(unittest.TestCase):
    def test_arithmetic_contexts(self):
        executable = '$( ! missing | cat )'
        literals = ("'" + executable + "'", "'${x:-" + executable + "}'",
                    "'`! missing | cat`'", "']" + executable + "['")
        expressions = [('a[' + key + ']', False) for key in literals]
        expressions += [('a[' + executable + ']', True),
                        ('a["' + executable + '"]', True),
                        ('a[`! missing | cat`]', True),
                        ('a[${x:-' + executable + '}]', True),
                        (executable, True), ("'" + executable + "'", True),
                        ('a[' + literals[0] + '] + ' + executable, True),
                        ('a[b[' + literals[0] + ']]', False),
                        ('a[b[' + executable + ']]', True)]
        # Subscript operands inherit word quoting recursively. Double quotes
        # and nested arithmetic expansions establish their own contexts.
        for op in ('-', ':-', '+', ':+', '=', ':=', '?', ':?'):
            for operand in ("'" + executable + "'", "'`! missing | cat`'"):
                nested = '${x' + op + operand + '}'
                deep = '${y' + op + nested + '}'
                expressions.extend((('a[' + nested + ']', False),
                                    ('a[' + deep + ']', False),
                                    ('a["' + nested + '"]', True),
                                    ('a[$(( ' + nested + ' ))]', True)))
            expressions.append(('a[${x' + op + executable + '}]', True))
        for expression, want in expressions:
            forms = (
                '(( ' + expression + ' == 0 ))',
                'rc=$(( ' + expression + ' ))',
                'rc="$(( ' + expression + ' ))"',
                'rc=$[ ' + expression + ' ]',
                'rc="$[ ' + expression + ' ]"',
                'for (( i=' + expression + '; i<1; i++ )); do true; done',
                'for (( i=0; i<' + expression + '; i++ )); do true; done',
                'for (( i=0; i<1; i+=' + expression + ' )); do true; done',
                'rc="${x: ' + expression + '}"',
                'rc="${x:0: ' + expression + '}"',
                'cat <<END\n$(( ' + expression + ' ))\nEND',
                'cat <<END\n$[ ' + expression + ' ]\nEND',
                'cat <<END\n${x: ' + expression + '}\nEND',
                'cat <<END\n${x:0: ' + expression + '}\nEND',
            )
            for form in forms:
                command = 'declare -A a b; ' + form
                for table in ('roles', 'live'):
                    with self.subTest(command=command, table=table):
                        doc = {'family': 'el', 'roles': {'probe': command}} if table == 'roles' else {
                            'family': 'el', 'vm': {'live': {'probe': command}}}
                        errors = check.box_shell_errors(doc)
                        self.assertFalse(any('lint failed' in e for e in errors), errors)
                        self.assertEqual(any('negated pipelines' in e for e in errors), want, errors)


if __name__ == '__main__':
    unittest.main()
