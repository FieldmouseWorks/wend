"""Parameter operands select their own expansion rules, recursively."""
import unittest
import check


class ParameterTests(unittest.TestCase):
    def test_parameter_operand_roles(self):
        executable = '$( ! missing | cat )'
        literal = "'" + executable + "'"
        cases = []
        unquoted_literals = set()
        # Patterns keep their quotes, with or without enclosing double quotes.
        for op in ('#', '##', '%', '%%', '^', '^^', ',', ',,'):
            cases.extend((('${x' + op + literal + '}', False),
                          ('${x' + op + executable + '}', True)))
        # Search and replacement operands are separate pattern/word contexts.
        for op in ('/', '//', '/#', '/%'):
            for search, replacement, want in ((literal, 'z', False), ('a', literal, False),
                                               (executable, 'z', True), ('a', executable, True)):
                cases.append(('${x' + op + search + '/' + replacement + '}', want))
        cases.extend((('${x[' + literal + ']}', False),
                      ('${x[' + executable + ']}', True)))
        # These operands inherit surrounding quotes rather than pattern rules.
        for op in ('-', ':-', '+', ':+', '=', ':=', '?', ':?'):
            for body in ('${x' + op + literal + '}', '${x' + op + '${y:-' + literal + '}}'):
                cases.append((body, True))
                unquoted_literals.add(body)
        # Arithmetic expands substitutions even in single-quoted expression text.
        cases.extend((('${x:0:' + literal + '}', True),
                      ('${x:' + literal + '}', True),
                      ('${x:0:$( ! missing | cat )}', True)))
        # Switch context at each nested operand, rather than propagating a flag
        # across an entire parameter expression.
        cases.extend((('${x:-${y#' + literal + '}}', False),
                      ('${x#${y:-' + literal + '}}', False),
                      ('${x/a/${y:-' + literal + '}}', False),
                      ('${x:-${y#' + executable + '}}', True),
                      ('${x#${y:-' + executable + '}}', True),
                      ('${x/a/${y:-' + executable + '}}', True),
                      ('${x^^${y:-' + literal + '}}', False),
                      ('${x^^${y:-' + executable + '}}', True),
                      ('${x[${y:-' + literal + '}]}', False),
                      ('${x[${y:-' + executable + '}]}', True)))
        for expression, want in cases:
            for style, command in (('quoted', 'rc="' + expression + '"'),
                                   ('heredoc', 'cat <<END\n' + expression + '\nEND'),
                                   ('unquoted', 'rc=' + expression)):
                expected = False if style == 'unquoted' and expression in unquoted_literals else want
                if expression.startswith('${x['):
                    command = 'declare -A x; ' + command
                for table in ('roles', 'live'):
                    with self.subTest(command=command, table=table):
                        doc = {'family': 'el', 'roles': {'probe': command}} if table == 'roles' else {
                            'family': 'el', 'vm': {'live': {'probe': command}}}
                        errors = check.box_shell_errors(doc)
                        self.assertFalse(any('lint failed' in e for e in errors), errors)
                        self.assertEqual(any('negated pipelines' in e for e in errors), expected, errors)


if __name__ == '__main__':
    unittest.main()
