//! The terminal, for the full-screen first run (`screens`): what it can show, raw input turned into keys and
//! clicks, and a grid of styled cells written out with the escape sequences every terminal emulator of the last
//! thirty years understands. No terminfo and no dependency: the emulator is on the person's desk, however old the
//! server is, and the sequences here are the ones xterm, PuTTY, tmux, screen and the Linux console share.

use std::io::Write;

/// What the terminal can show beyond plain text.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Caps {
    pub colour: bool,
    /// Line-drawing characters and arrows; without it, `+`, `-`, `|` and `->`.
    pub unicode: bool,
}

/// What this terminal can show, or None when it shouldn't be driven full-screen at all: not a terminal, TERM unset
/// or `dumb`, or the person asked for plain text.
pub fn caps(env: &dyn Fn(&str) -> Option<String>, is_terminal: bool) -> Option<Caps> {
    let term = env("TERM").unwrap_or_default();
    if !is_terminal || term.is_empty() || term == "dumb" || env("WEND_PLAIN").is_some() {
        return None;
    }
    // the locale says whether the terminal was told to expect UTF-8; sudo often strips it, and then plain it is
    let locale = ["LC_ALL", "LC_CTYPE", "LANG"]
        .iter()
        .find_map(|k| env(k).filter(|v| !v.is_empty()))
        .unwrap_or_default()
        .to_ascii_lowercase();
    Some(Caps {
        colour: env("NO_COLOR").is_none(),
        // the Linux console's font has few of the glyphs, whatever the locale says
        unicode: term != "linux" && (locale.contains("utf-8") || locale.contains("utf8")),
    })
}

/// Columns and rows, or 80 by 24 when the terminal won't say.
pub fn size() -> (usize, usize) {
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    // SAFETY: TIOCGWINSZ fills the winsize it is given.
    if unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) } == 0 && ws.ws_col > 0 && ws.ws_row > 0 {
        (ws.ws_col as usize, ws.ws_row as usize)
    } else {
        (80, 24)
    }
}

const ENTER: &str = "\x1b[?1049h\x1b[?25l";
const LEAVE: &str = "\x1b[0m\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l";
const MOUSE_ON: &str = "\x1b[?1000h\x1b[?1006h";
const MOUSE_OFF: &str = "\x1b[?1006l\x1b[?1000l";

/// The terminal held full-screen: its own screen, no echo, keys one at a time. Dropping it gives the terminal back
/// as it was, whatever happened in between.
pub struct Raw {
    saved: libc::termios,
}

impl Raw {
    pub fn enter() -> Option<Raw> {
        let mut t: libc::termios = unsafe { std::mem::zeroed() };
        // SAFETY: tcgetattr fills the termios it is given.
        if unsafe { libc::tcgetattr(0, &mut t) } != 0 {
            return None;
        }
        let saved = t;
        // no line editing, no echo, and Ctrl-C arrives as a key, so the terminal is always given back
        t.c_lflag &= !(libc::ICANON | libc::ECHO | libc::ISIG | libc::IEXTEN);
        t.c_iflag &= !(libc::IXON | libc::ICRNL | libc::INLCR);
        // a read returns what is there, or nothing after 0.2 s: that is the clock a resize is noticed on
        t.c_cc[libc::VMIN] = 0;
        t.c_cc[libc::VTIME] = 2;
        // SAFETY: t is a termios tcgetattr filled.
        if unsafe { libc::tcsetattr(0, libc::TCSAFLUSH, &t) } != 0 {
            return None;
        }
        put(&format!("{ENTER}{MOUSE_ON}"));
        Some(Raw { saved })
    }

    /// Mouse reporting on or off. While it is on, the terminal sends clicks here and can't select text.
    pub fn mouse(&self, on: bool) {
        put(if on { MOUSE_ON } else { MOUSE_OFF });
    }
}

impl Drop for Raw {
    fn drop(&mut self) {
        put(LEAVE);
        // SAFETY: saved is the termios this terminal had.
        unsafe { libc::tcsetattr(0, libc::TCSAFLUSH, &self.saved) };
    }
}

/// Throw away what was typed and not read yet: keys pressed while something else was going on aren't meant for
/// the screen that comes after it.
pub fn discard_input() {
    // SAFETY: tcflush takes a file descriptor and a queue selector.
    unsafe { libc::tcflush(0, libc::TCIFLUSH) };
}

pub fn put(s: &str) {
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(s.as_bytes());
    let _ = out.flush();
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Key {
    Up,
    Down,
    Left,
    Right,
    PageUp,
    PageDown,
    Home,
    End,
    Tab,
    BackTab,
    Enter,
    Esc,
    /// Ctrl-C or Ctrl-D: leave now.
    Stop,
    Char(char),
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Event {
    Key(Key),
    /// A left click, at a cell counted from 0.
    Click {
        col: usize,
        row: usize,
    },
    /// The wheel: negative is up.
    Wheel(i32),
}

/// What the bytes a terminal sent mean, and how many of them that used up. Bytes that begin a sequence the rest of
/// which hasn't arrived stay unused; a lone ESC at the end is the Esc key, since nothing follows it within a read.
pub fn decode(b: &[u8]) -> (Vec<Event>, usize) {
    let mut out = vec![];
    let mut i = 0;
    while i < b.len() {
        let rest = &b[i..];
        let (ev, used) = match rest {
            [0x1b, b'[', b'<', tail @ ..] => match sgr_mouse(tail) {
                Some((ev, n)) => (ev, 3 + n),
                None if tail.iter().any(|c| matches!(c, b'M' | b'm')) => (None, rest.len()),
                None => break,
            },
            [0x1b, b'[', b'M', tail @ ..] => match tail {
                [btn, x, y, ..] => (
                    button(btn.wrapping_sub(32) as u32, *x as usize - 32, *y as usize - 32, true),
                    6,
                ),
                _ => break,
            },
            [0x1b, b'[' | b'O', tail @ ..] => {
                // parameters, then one final byte
                let n = tail.iter().take_while(|c| c.is_ascii_digit() || **c == b';').count();
                let Some(last) = tail.get(n) else { break };
                let key = match (&tail[..n], last) {
                    (_, b'A') => Some(Key::Up),
                    (_, b'B') => Some(Key::Down),
                    (_, b'C') => Some(Key::Right),
                    (_, b'D') => Some(Key::Left),
                    (_, b'H') => Some(Key::Home),
                    (_, b'F') => Some(Key::End),
                    (_, b'Z') => Some(Key::BackTab),
                    (b"5", b'~') => Some(Key::PageUp),
                    (b"6", b'~') => Some(Key::PageDown),
                    (b"1" | b"7", b'~') => Some(Key::Home),
                    (b"4" | b"8", b'~') => Some(Key::End),
                    _ => None,
                };
                (key.map(Event::Key), 2 + n + 1)
            }
            [0x1b] => (Some(Event::Key(Key::Esc)), 1),
            // ESC and then a key is Alt with it: the key alone
            [0x1b, ..] => (None, 1),
            [b'\r' | b'\n', ..] => (Some(Event::Key(Key::Enter)), 1),
            [b'\t', ..] => (Some(Event::Key(Key::Tab)), 1),
            [0x03 | 0x04, ..] => (Some(Event::Key(Key::Stop)), 1),
            [c, ..] if (0x20..0x7f).contains(c) => (Some(Event::Key(Key::Char(*c as char))), 1),
            _ => (None, 1),
        };
        out.extend(ev);
        i += used;
    }
    (out, i)
}

/// `b;x;yM` (pressed) or `b;x;ym` (let go), after `ESC [ <`.
fn sgr_mouse(tail: &[u8]) -> Option<(Option<Event>, usize)> {
    let end = tail.iter().position(|c| matches!(c, b'M' | b'm'))?;
    let text = std::str::from_utf8(&tail[..end]).ok()?;
    let mut n = text.split(';').map(|p| p.parse::<usize>().ok());
    let (btn, x, y) = (n.next()??, n.next()??, n.next()??);
    Some((button(btn as u32, x, y, tail[end] == b'M'), end + 1))
}

fn button(btn: u32, x: usize, y: usize, pressed: bool) -> Option<Event> {
    match btn {
        64 => Some(Event::Wheel(-3)),
        65 => Some(Event::Wheel(3)),
        0 if pressed && x > 0 && y > 0 => Some(Event::Click { col: x - 1, row: y - 1 }),
        _ => None,
    }
}

/// Read what the terminal sent since the last call, waiting up to 0.2 s for something. None when it has gone away.
pub fn events(pending: &mut Vec<u8>) -> Option<Vec<Event>> {
    let mut buf = [0u8; 256];
    // SAFETY: read fills at most buf.len() bytes of buf.
    let n = unsafe { libc::read(0, buf.as_mut_ptr().cast(), buf.len()) };
    if n < 0 {
        let interrupted = std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted;
        return interrupted.then(Vec::new);
    }
    pending.extend_from_slice(&buf[..n as usize]);
    let (evs, used) = decode(pending);
    pending.drain(..used);
    Some(evs)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Style {
    Plain,
    Bold,
    Dim,
    Good,
    Warn,
    Bad,
    /// A button that has the focus.
    Focus,
}

impl Style {
    fn sgr(self, colour: bool) -> &'static str {
        match (self, colour) {
            (Style::Plain, _) => "",
            (Style::Bold, _) => "1",
            (Style::Focus, _) => "1;7",
            (Style::Dim, true) => "2",
            (Style::Good, true) => "1;32",
            (Style::Warn, true) => "1;33",
            (Style::Bad, true) => "1;31",
            // without colour, the answer is still set apart
            (Style::Good | Style::Warn | Style::Bad, false) => "1",
            (Style::Dim, false) => "",
        }
    }
}

/// A screen of styled cells, drawn into by position and written out whole.
pub struct Canvas {
    pub w: usize,
    pub h: usize,
    cells: Vec<(char, Style)>,
}

impl Canvas {
    pub fn new(w: usize, h: usize) -> Canvas {
        Canvas {
            w,
            h,
            cells: vec![(' ', Style::Plain); w * h],
        }
    }

    /// Write `text` from (col, row) rightwards; what doesn't fit is dropped.
    pub fn text(&mut self, col: usize, row: usize, text: &str, style: Style) {
        if row >= self.h {
            return;
        }
        for (i, ch) in text.chars().enumerate() {
            if col + i >= self.w {
                break;
            }
            let ch = if ch.is_control() { ' ' } else { ch };
            self.cells[row * self.w + col + i] = (ch, style);
        }
    }

    /// One row as plain text, trailing spaces dropped.
    #[cfg(test)]
    pub fn row(&self, row: usize) -> String {
        let cells = &self.cells[row * self.w..(row + 1) * self.w];
        cells.iter().map(|c| c.0).collect::<String>().trim_end().to_string()
    }

    #[cfg(test)]
    pub fn style_at(&self, col: usize, row: usize) -> Style {
        self.cells[row * self.w + col].1
    }

    /// The whole screen as the bytes that draw it.
    pub fn render(&self, caps: Caps) -> String {
        let mut s = String::from("\x1b[0m\x1b[2J");
        for r in 0..self.h {
            s += &format!("\x1b[{};1H", r + 1);
            let mut current = Style::Plain;
            let cells = &self.cells[r * self.w..(r + 1) * self.w];
            // don't write the blank tail: on the last row it would scroll the screen
            let used = cells
                .iter()
                .rposition(|c| *c != (' ', Style::Plain))
                .map_or(0, |p| p + 1);
            for (ch, style) in &cells[..used] {
                if *style != current {
                    s += &format!("\x1b[0m\x1b[{}m", style.sgr(caps.colour));
                    current = *style;
                }
                s.push(*ch);
            }
            s += "\x1b[0m";
        }
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env<'a>(vars: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| vars.iter().find(|(n, _)| *n == k).map(|(_, v)| v.to_string())
    }

    #[test]
    fn says_what_the_terminal_can_show() {
        let full = Some(Caps {
            colour: true,
            unicode: true,
        });
        assert_eq!(
            caps(&env(&[("TERM", "xterm-256color"), ("LANG", "en_US.UTF-8")]), true),
            full
        );
        // sudo stripped the locale: colour, and plain line drawing
        assert_eq!(
            caps(&env(&[("TERM", "xterm")]), true),
            Some(Caps {
                colour: true,
                unicode: false
            })
        );
        assert_eq!(
            caps(&env(&[("TERM", "linux"), ("LANG", "en_US.UTF-8")]), true),
            Some(Caps {
                colour: true,
                unicode: false
            })
        );
        assert_eq!(
            caps(
                &env(&[
                    ("TERM", "xterm"),
                    ("LC_ALL", "C"),
                    ("LANG", "en_US.UTF-8"),
                    ("NO_COLOR", "1")
                ]),
                true
            ),
            Some(Caps {
                colour: false,
                unicode: false
            })
        );
        // nothing to drive full-screen: no terminal, no TERM, a dumb one, or plain asked for
        assert_eq!(caps(&env(&[("TERM", "xterm")]), false), None);
        assert_eq!(caps(&env(&[]), true), None);
        assert_eq!(caps(&env(&[("TERM", "dumb")]), true), None);
        assert_eq!(caps(&env(&[("TERM", "xterm"), ("WEND_PLAIN", "1")]), true), None);
    }

    #[test]
    fn reads_keys() {
        use Key::*;
        let keys = |b: &[u8]| decode(b).0;
        let k = |key| Event::Key(key);
        assert_eq!(keys(b"\x1b[A\x1b[B\x1b[C\x1b[D"), [k(Up), k(Down), k(Right), k(Left)]);
        // application mode, and the forms with parameters
        assert_eq!(keys(b"\x1bOA\x1bOB\x1b[1;5C"), [k(Up), k(Down), k(Right)]);
        assert_eq!(
            keys(b"\x1b[5~\x1b[6~\x1b[H\x1b[F\x1b[1~\x1b[4~"),
            [k(PageUp), k(PageDown), k(Home), k(End), k(Home), k(End)]
        );
        assert_eq!(
            keys(b"\t\x1b[Z\r\nq "),
            [k(Tab), k(BackTab), k(Enter), k(Enter), k(Char('q')), k(Char(' '))]
        );
        assert_eq!(keys(b"\x03\x04"), [k(Stop), k(Stop)]);
        assert_eq!(keys(b"\x1b"), [k(Esc)]);
        // a function key it has no use for is swallowed whole, not typed as its letters
        assert_eq!(keys(b"\x1b[15~x"), [k(Char('x'))]);
        assert_eq!(keys(b"\x7f\x00"), []);
    }

    #[test]
    fn reads_the_mouse() {
        let click = |col, row| Event::Click { col, row };
        // SGR: a press counts, the release doesn't; cells arrive counted from 1
        assert_eq!(decode(b"\x1b[<0;12;5M\x1b[<0;12;5m").0, [click(11, 4)]);
        assert_eq!(
            decode(b"\x1b[<64;1;1M\x1b[<65;1;1M").0,
            [Event::Wheel(-3), Event::Wheel(3)]
        );
        // other buttons, and a drag, are nothing
        assert_eq!(decode(b"\x1b[<2;3;3M\x1b[<32;3;3M").0, []);
        // the older form: three bytes, each 32 more than its value
        assert_eq!(decode(b"\x1b[M\x20\x2c\x25").0, [click(11, 4)]);
        assert_eq!(decode(b"\x1b[M\x60\x21\x21").0, [Event::Wheel(-3)]);
    }

    #[test]
    fn keeps_half_a_sequence_for_the_next_read() {
        for (bytes, used) in [
            (&b"q\x1b["[..], 1),
            (b"\x1b[<0;12", 0),
            (b"\x1b[M\x20", 0),
            (b"\x1b[1;", 0),
        ] {
            assert_eq!(decode(bytes).1, used, "{bytes:?}");
        }
        let mut pending = b"\x1b[<0;12".to_vec();
        pending.extend_from_slice(b";5M");
        assert_eq!(
            decode(&pending),
            (vec![Event::Click { col: 11, row: 4 }], pending.len())
        );
        // a mouse report that makes no sense is dropped, not left to block everything after it
        assert_eq!(decode(b"\x1b[<x;yMq"), (vec![], 8));
    }

    #[test]
    fn draws_cells_and_writes_them_out() {
        let mut c = Canvas::new(10, 2);
        c.text(1, 0, "wend", Style::Bold);
        c.text(8, 1, "abcdef", Style::Plain);
        c.text(0, 5, "off the bottom", Style::Plain);
        assert_eq!((c.row(0), c.row(1)), (" wend".to_string(), "        ab".to_string()));
        assert_eq!(c.style_at(1, 0), Style::Bold);
        let colour = c.render(Caps {
            colour: true,
            unicode: false,
        });
        assert!(
            colour.starts_with("\x1b[0m\x1b[2J\x1b[1;1H \x1b[0m\x1b[1mwend\x1b[0m\x1b[2;1H"),
            "{colour:?}"
        );
        // a control character in the text can't move the cursor or change the screen
        c.text(0, 1, "a\x1b[2Jb", Style::Plain);
        assert_eq!(c.row(1), "a [2Jb  ab");
        // no colour: the answer is bold, and nothing is dimmed
        let mut m = Canvas::new(4, 1);
        m.text(0, 0, "ok", Style::Good);
        m.text(2, 0, "no", Style::Dim);
        assert!(m
            .render(Caps {
                colour: false,
                unicode: false
            })
            .contains("\x1b[1mok\x1b[0m\x1b[mno"));
    }
}
