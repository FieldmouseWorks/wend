//! Parse shell syntax without executing it. Only a Pipeline's bang and command
//! count determine the lint; nested programs and expansions have separate scope.
use brush_parser::{word, Parser, ParserOptions};
use serde_json::Value;
use std::io::{self, Read};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn program(source: &str) -> Result<bool> {
    let ast = Parser::new(source.as_bytes(), &ParserOptions::default()).parse_program()?;
    syntax(&serde_json::to_value(ast)?)
}

// Walk the parser's serialized AST, including every compound command variant.
// Word and heredoc nodes need expansion parsing, rather than shell parsing.
fn syntax(v: &Value) -> Result<bool> {
    if let Some(o) = v.as_object() {
        if o.get("bang") == Some(&Value::Bool(true))
            && o.get("seq")
                .and_then(Value::as_array)
                .is_some_and(|s| s.len() > 1)
        {
            return Ok(true);
        }
        if let Some(doc) = o.get("doc") {
            return if o.get("requires_expansion") == Some(&Value::Bool(true)) {
                expansions(doc["value"].as_str().ok_or("missing heredoc text")?, true)
            } else {
                Ok(false)
            };
        }
        if let Some(s) = o.get("value").and_then(Value::as_str) {
            // Word has loc; an arithmetic expression has only value. Neither
            // expression operators nor literal text are shell pipelines.
            return expansions(s, !o.contains_key("loc"));
        }
        for child in o.values() {
            if syntax(child)? {
                return Ok(true);
            }
        }
    } else if let Some(a) = v.as_array() {
        for child in a {
            if syntax(child)? {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

fn expansions(s: &str, heredoc: bool) -> Result<bool> {
    let options = ParserOptions::default();
    let parts = if heredoc {
        word::parse_heredoc(s, &options)?
    } else {
        word::parse(s, &options)?
    };
    pieces(parts, heredoc)
}

fn pieces(parts: Vec<word::WordPieceWithSource>, quoted: bool) -> Result<bool> {
    for part in parts {
        use word::WordPiece::*;
        let found = match part.piece {
            CommandSubstitution(s) | BackquotedCommandSubstitution(s) => program(&s)?,
            DoubleQuotedSequence(parts) | GettextDoubleQuotedSequence(parts) => {
                pieces(parts, true)?
            }
            ArithmeticExpression(expr) => expansions(&expr.value, true)?,
            ParameterExpansion(expr) => parameter(&expr, quoted)?,
            _ => false,
        };
        if found {
            return Ok(true);
        }
    }
    Ok(false)
}

// Operand syntax is selected by its role, not just by the enclosing quotes.
// Defaults inherit double-quote/heredoc context; patterns, replacement words
// and array indices retain their own literal quotes. Arithmetic operands expand
// substitutions before arithmetic evaluation even when quote characters occur.
fn optional(s: &Option<String>, quoted: bool) -> Result<bool> {
    s.as_deref().map_or(Ok(false), |s| expansions(s, quoted))
}

fn parameter(expr: &word::ParameterExpr, quoted: bool) -> Result<bool> {
    use word::ParameterExpr::*;
    let (parameter, found) = match expr {
        Parameter { parameter, .. }
        | ParameterLength { parameter, .. }
        | Transform { parameter, .. } => (parameter, false),
        UseDefaultValues {
            parameter,
            default_value,
            ..
        }
        | AssignDefaultValues {
            parameter,
            default_value,
            ..
        } => (parameter, optional(default_value, quoted)?),
        UseAlternativeValue {
            parameter,
            alternative_value,
            ..
        } => (parameter, optional(alternative_value, quoted)?),
        IndicateErrorIfNullOrUnset {
            parameter,
            error_message,
            ..
        } => (parameter, optional(error_message, quoted)?),
        RemoveSmallestSuffixPattern {
            parameter, pattern, ..
        }
        | RemoveLargestSuffixPattern {
            parameter, pattern, ..
        }
        | RemoveSmallestPrefixPattern {
            parameter, pattern, ..
        }
        | RemoveLargestPrefixPattern {
            parameter, pattern, ..
        }
        | UppercaseFirstChar {
            parameter, pattern, ..
        }
        | UppercasePattern {
            parameter, pattern, ..
        }
        | LowercaseFirstChar {
            parameter, pattern, ..
        }
        | LowercasePattern {
            parameter, pattern, ..
        } => (parameter, optional(pattern, false)?),
        ReplaceSubstring {
            parameter,
            pattern,
            replacement,
            ..
        } => (
            parameter,
            expansions(pattern, false)? || optional(replacement, false)?,
        ),
        Substring {
            parameter,
            offset,
            length,
            ..
        } => (
            parameter,
            expansions(&offset.value, true)?
                || match length {
                    Some(expr) => expansions(&expr.value, true)?,
                    None => false,
                },
        ),
        VariableNames { .. } | MemberKeys { .. } => return Ok(false),
    };
    if found {
        return Ok(true);
    }
    match parameter {
        word::Parameter::NamedWithIndex { index, .. } => expansions(index, false),
        word::Parameter::Positional(_)
        | word::Parameter::Special(_)
        | word::Parameter::Named(_)
        | word::Parameter::NamedWithAllIndices { .. } => Ok(false),
    }
}

fn main() {
    let result = (|| -> Result<bool> {
        let mut source = String::new();
        io::stdin().read_to_string(&mut source)?;
        program(&source)
    })();
    match result {
        Ok(found) => println!("{}", if found { "negated" } else { "ok" }),
        Err(e) => {
            eprintln!("shell lint cannot parse: {e}");
            std::process::exit(2);
        }
    }
}
