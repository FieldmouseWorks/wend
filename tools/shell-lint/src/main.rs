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
    for part in parts {
        use word::WordPiece::*;
        let found = match part.piece {
            CommandSubstitution(s) | BackquotedCommandSubstitution(s) => program(&s)?,
            DoubleQuotedSequence(parts) | GettextDoubleQuotedSequence(parts) => {
                expansion_values(&serde_json::to_value(parts)?)?
            }
            ArithmeticExpression(expr) => expansions(&expr.value, true)?,
            ParameterExpansion(expr) => parameter_values(&serde_json::to_value(expr)?)?,
            _ => false,
        };
        if found {
            return Ok(true);
        }
    }
    Ok(false)
}

fn expansion_values(v: &Value) -> Result<bool> {
    if let Some(o) = v.as_object() {
        for (key, child) in o {
            match key.as_str() {
                "CommandSubstitution" | "BackquotedCommandSubstitution" => {
                    if program(child.as_str().ok_or("missing substitution")?)? {
                        return Ok(true);
                    }
                }
                "Text" | "SingleQuotedText" | "AnsiCQuotedText" | "EscapeSequence" => (),
                "ArithmeticExpression" => {
                    if syntax(child)? {
                        return Ok(true);
                    }
                }
                "ParameterExpansion" => {
                    if parameter_values(child)? {
                        return Ok(true);
                    }
                }
                _ => {
                    if expansion_values(child)? {
                        return Ok(true);
                    }
                }
            }
        }
    } else if let Some(a) = v.as_array() {
        for child in a {
            if expansion_values(child)? {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

fn parameter_values(v: &Value) -> Result<bool> {
    match v {
        Value::String(s) => expansions(s, false),
        Value::Array(a) => {
            for c in a {
                if parameter_values(c)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        Value::Object(o) => {
            for c in o.values() {
                if parameter_values(c)? {
                    return Ok(true);
                }
            }
            Ok(false)
        }
        _ => Ok(false),
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
