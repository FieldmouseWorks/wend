//! The first run, full-screen: the words `guide` has, in a frame with buttons, for a terminal that can show them.
//! Arrow keys, Tab and Enter work everywhere; a click works wherever the terminal program reports the mouse, which
//! is nearly all of them. `draw` is the whole of what is shown and `step` the whole of what a key or click does, so
//! both are tested without a terminal.

use crate::answer::{self, Outcome};
use crate::guide::{self, Checked};
use crate::term::{self, Canvas, Caps, Event, Key, Raw, Style};

/// The smallest terminal it draws on: the size every terminal starts at.
const MIN: (usize, usize) = (80, 24);
/// A frame wider than this is harder to read than a centred one.
const MAX_WIDTH: usize = 96;

/// Whether the terminal is big enough to start full-screen (else the plain run does it).
pub fn fits() -> bool {
    let (w, h) = term::size();
    w >= MIN.0 && h >= MIN.1
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Screen {
    Welcome,
    /// What the check reads, from wherever it was asked.
    Reads,
    Checking,
    Result,
    Report,
    Share,
    Failed,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Action {
    Check,
    Reads,
    Report,
    Share,
    Back,
    Quit,
}

pub struct Ui {
    pub screen: Screen,
    /// Where Back goes.
    pub back: Screen,
    /// Which button has the focus.
    pub focus: usize,
    /// How many lines of the text are scrolled off the top.
    pub scroll: usize,
    /// How long the check has been running, in fifths of a second, while it runs.
    pub ticks: usize,
}

impl Ui {
    pub fn new() -> Ui {
        Ui {
            screen: Screen::Welcome,
            back: Screen::Welcome,
            focus: 0,
            scroll: 0,
            ticks: 0,
        }
    }

    fn go(&mut self, to: Screen) {
        if !matches!(to, Screen::Welcome | Screen::Result) {
            self.back = match self.screen {
                Screen::Welcome | Screen::Result => self.screen,
                _ => self.back,
            };
        }
        (self.screen, self.focus, self.scroll) = (to, 0, 0);
    }
}

fn buttons(screen: Screen) -> &'static [(&'static str, Action)] {
    match screen {
        Screen::Welcome => &[
            ("Check this machine", Action::Check),
            ("What will it read?", Action::Reads),
            ("Quit", Action::Quit),
        ],
        Screen::Result => &[
            ("Read the report", Action::Report),
            ("Share it", Action::Share),
            ("What was read", Action::Reads),
            ("Quit", Action::Quit),
        ],
        Screen::Reads | Screen::Report | Screen::Share => &[("Back", Action::Back)],
        Screen::Failed => &[("Quit", Action::Quit)],
        Screen::Checking => &[],
    }
}

/// A button's place on the screen, for a click to land on.
#[derive(Debug, PartialEq)]
pub struct Hit {
    pub row: usize,
    pub from: usize,
    pub to: usize,
    pub action: Action,
}

pub struct Frame {
    pub canvas: Canvas,
    pub hits: Vec<Hit>,
    /// How far the text can scroll, and how many lines of it show at once.
    pub max_scroll: usize,
    pub page: usize,
    /// Text for the terminal to wrap by itself, from the start of this row: a link, which has to come out of a
    /// selection in one piece.
    pub raw: Option<(usize, String)>,
}

fn heading(text: &str) -> (String, Style) {
    (text.to_string(), Style::Bold)
}

fn plain(text: &str) -> impl Iterator<Item = (String, Style)> + '_ {
    text.lines().map(|l| (l.to_string(), Style::Plain))
}

/// The lines a screen shows, each with how it is set apart.
fn lines(
    screen: Screen,
    ticks: usize,
    made: Option<&Checked>,
    error: Option<&str>,
    caps: Caps,
) -> Vec<(String, Style)> {
    let mut out = vec![];
    match (screen, made) {
        (Screen::Welcome, _) => {
            out.push(heading("Find out whether this machine can be upgraded."));
            out.push((String::new(), Style::Plain));
            out.extend(plain(&guide::wrap(guide::FIRST_STEP, "  ")));
            out.push((String::new(), Style::Plain));
            for p in guide::PROMISES {
                out.push((
                    format!("    {} {p}", if caps.unicode { "\u{2022}" } else { "-" }),
                    Style::Plain,
                ));
            }
            out.push((String::new(), Style::Plain));
            out.push((
                "  It takes a few seconds, up to a minute on a busy machine.".to_string(),
                Style::Plain,
            ));
        }
        (Screen::Reads, _) => {
            out.push(heading("What the check reads"));
            out.push((String::new(), Style::Plain));
            out.extend(plain(&guide::reads()));
        }
        (Screen::Checking, _) => {
            // a mark that moves and a count that grows: on a busy machine this takes a while, and it should not
            // look stuck
            let mark = ["|", "/", "-", "\\"][ticks % 4];
            out.push(heading(&format!("Checking this machine  {mark}")));
            out.push((String::new(), Style::Plain));
            out.push(("  Reading only. Nothing is being changed.".to_string(), Style::Plain));
            out.push((String::new(), Style::Plain));
            out.push((
                format!(
                    "  {} s so far. It can take up to a minute on a busy machine.",
                    ticks / 5
                ),
                Style::Plain,
            ));
        }
        (Screen::Result, Some(c)) => {
            let r = &c.report;
            out.push(heading("THIS MACHINE"));
            out.extend(plain(&answer::machine(&r.sys, &r.facts)));
            out.push((String::new(), Style::Plain));
            out.push(heading("CAN WEND UPGRADE IT?"));
            let answer = match answer::outcome(&r.judged) {
                Outcome::Yes => Style::Good,
                Outcome::Partway => Style::Warn,
                Outcome::No => Style::Bad,
                Outcome::NoPath => Style::Bold,
            };
            for (i, l) in answer::verdict(&r.sys, &r.judged, c.paths.as_deref())
                .lines()
                .enumerate()
            {
                // the answer in a few words, set apart, and the path with real arrows where there are any
                let arrow = l.starts_with("      ") && l.contains(" -> ");
                let text = if arrow && caps.unicode {
                    l.replace(" -> ", " \u{2192} ")
                } else {
                    l.to_string()
                };
                out.push((
                    text,
                    if i == 0 {
                        answer
                    } else if arrow {
                        Style::Bold
                    } else {
                        Style::Plain
                    },
                ));
            }
            out.push((String::new(), Style::Plain));
            out.push((
                "  Nothing on this machine was changed, and nothing was sent anywhere.".to_string(),
                Style::Dim,
            ));
            out.push((format!("  The report: {}", c.file), Style::Dim));
        }
        (Screen::Report, Some(c)) => {
            out.push(heading(&format!("The report, as saved in {}", c.report.file)));
            out.push((String::new(), Style::Plain));
            for l in c.report.full.lines() {
                // a long line is cut into screen-wide pieces, so nothing of what would be sent is hidden
                let chars: Vec<char> = l.chars().collect();
                if chars.is_empty() {
                    out.push((String::new(), Style::Plain));
                }
                for piece in chars.chunks(guide::WIDTH - 2) {
                    out.push((format!("  {}", piece.iter().collect::<String>()), Style::Plain));
                }
            }
        }
        (Screen::Share, Some(c)) => {
            out.push(heading("SHARING IT (your choice)"));
            out.extend(plain(&guide::sharing(&c.file)));
        }
        _ => {
            out.push(("The check could not finish.".to_string(), Style::Bad));
            out.push((String::new(), Style::Plain));
            out.extend(plain(&guide::wrap(error.unwrap_or("It did not say why."), "  ")));
            out.push((String::new(), Style::Plain));
            out.push(("  Nothing on this machine was changed.".to_string(), Style::Plain));
        }
    }
    out
}

/// The line of help under the frame. Each fits an 80-column terminal.
fn hint(screen: Screen, scrolls: bool) -> &'static str {
    match (screen, scrolls) {
        (Screen::Checking, _) => "Ctrl-C: stop",
        (Screen::Share, _) => "Drag over the link to copy it. It is printed again when you quit.  Enter: back",
        (Screen::Reads | Screen::Report, true) => "Up, Down, PgUp, PgDn or the wheel: scroll    Enter or Esc: back",
        (Screen::Reads | Screen::Report, false) => "Enter or Esc: back",
        (_, true) => "Left/Right or the mouse: choose   Enter: select   Up/Down: scroll   q: quit",
        (_, false) => "Left, Right, Tab or the mouse: choose    Enter: select    q: quit",
    }
}

/// Everything a screen shows, for a terminal `w` by `h`.
pub fn draw(ui: &Ui, made: Option<&Checked>, error: Option<&str>, w: usize, h: usize, caps: Caps) -> Frame {
    let mut c = Canvas::new(w, h);
    let frame = |canvas| Frame {
        canvas,
        hits: vec![],
        max_scroll: 0,
        page: 0,
        raw: None,
    };
    if w < MIN.0 || h < MIN.1 {
        for (row, l) in [
            "wend needs a larger window:",
            "at least 80 columns by 24 rows.",
            "",
            "Enlarge this one, or press q",
            "and run: wend --plain",
        ]
        .iter()
        .enumerate()
        {
            c.text(0, row, l, if row == 0 { Style::Bold } else { Style::Plain });
        }
        return frame(c);
    }
    let (hz, vt, tl, tr, bl, br, lt, rt) = if caps.unicode {
        (
            '\u{2500}', '\u{2502}', '\u{250c}', '\u{2510}', '\u{2514}', '\u{2518}', '\u{251c}', '\u{2524}',
        )
    } else {
        ('-', '|', '+', '+', '+', '+', '+', '+')
    };
    let bw = w.min(MAX_WIDTH);
    let left = (w - bw) / 2;
    // rows: the title, the text, a rule, the buttons, the bottom, and under the frame a line of help
    let (rule, button_row, bottom, help) = (h - 4, h - 3, h - 2, h - 1);
    let (first, mut page) = (2, rule - 2);
    let text = lines(ui.screen, ui.ticks, made, error, caps);
    // the link goes under the frame's text with no frame beside it, for the terminal to wrap and a selection to
    // take whole
    let link = match (ui.screen, made) {
        (Screen::Share, Some(m)) => Some(m.link.clone()),
        _ => None,
    };
    let link_rows = link.as_ref().map_or(0, |l| l.chars().count().div_ceil(w) + 1);
    let boxed = link.is_none();
    if !boxed {
        page = page.saturating_sub(link_rows);
    }
    let line = |canvas: &mut Canvas, row: usize, a: char, b: char| {
        let mut s = String::from(a);
        s.extend(std::iter::repeat_n(hz, bw - 2));
        s.push(b);
        canvas.text(left, row, &s, Style::Plain);
    };
    if boxed {
        line(&mut c, 0, tl, tr);
        for row in 1..bottom {
            c.text(left, row, &vt.to_string(), Style::Plain);
            c.text(left + bw - 1, row, &vt.to_string(), Style::Plain);
        }
        line(&mut c, rule, lt, rt);
        line(&mut c, bottom, bl, br);
    } else {
        c.text(left, 0, &hz.to_string().repeat(bw), Style::Plain);
        c.text(left, rule, &hz.to_string().repeat(bw), Style::Plain);
    }
    c.text(left + 2, 0, &format!(" {} ", guide::NAME), Style::Bold);
    let tag = format!(" {} ", guide::TAGLINE);
    c.text(left + bw - 2 - tag.len(), 0, &tag, Style::Plain);
    let max_scroll = text.len().saturating_sub(page);
    let scroll = ui.scroll.min(max_scroll);
    for (i, (l, style)) in text.iter().skip(scroll).take(page).enumerate() {
        c.text(left + 2, first + i, l, *style);
    }
    if scroll > 0 {
        c.text(
            left + bw - 12,
            first - 1,
            if caps.unicode { " \u{25b2} more " } else { " ^ more " },
            Style::Dim,
        );
    }
    if scroll < max_scroll {
        c.text(
            left + bw - 12,
            rule,
            if caps.unicode { " \u{25bc} more " } else { " v more " },
            Style::Bold,
        );
    }
    let raw = link.map(|l| (first + text.len().min(page) + 1, l));
    let mut hits = vec![];
    let mut col = left + 2;
    for (i, (label, action)) in buttons(ui.screen).iter().enumerate() {
        let b = format!("[ {label} ]");
        c.text(
            col,
            button_row,
            &b,
            if i == ui.focus { Style::Focus } else { Style::Bold },
        );
        hits.push(Hit {
            row: button_row,
            from: col,
            to: col + b.len(),
            action: *action,
        });
        col += b.len() + 2;
    }
    c.text(left + 1, help, hint(ui.screen, max_scroll > 0), Style::Dim);
    Frame {
        canvas: c,
        hits,
        max_scroll,
        page,
        raw,
    }
}

/// What a key or a click does: move the focus or the text, and perhaps ask for something.
pub fn step(ui: &mut Ui, ev: Event, frame: &Frame) -> Option<Action> {
    let all = buttons(ui.screen);
    let sub = matches!(ui.screen, Screen::Reads | Screen::Report | Screen::Share);
    let leave = if sub { Action::Back } else { Action::Quit };
    let scroll = |ui: &mut Ui, by: isize| {
        ui.scroll = (ui.scroll.min(frame.max_scroll) as isize + by).clamp(0, frame.max_scroll as isize) as usize;
        None
    };
    match ev {
        Event::Key(Key::Stop) => Some(Action::Quit),
        _ if all.is_empty() => None,
        Event::Key(Key::Left | Key::BackTab) => {
            ui.focus = (ui.focus + all.len() - 1) % all.len();
            None
        }
        Event::Key(Key::Right | Key::Tab) => {
            ui.focus = (ui.focus + 1) % all.len();
            None
        }
        Event::Key(Key::Enter | Key::Char(' ')) => Some(all[ui.focus.min(all.len() - 1)].1),
        Event::Key(Key::Esc | Key::Char('q' | 'Q')) => Some(leave),
        Event::Key(Key::Up) => scroll(ui, -1),
        Event::Key(Key::Down) => scroll(ui, 1),
        Event::Key(Key::PageUp) => scroll(ui, -(frame.page as isize)),
        Event::Key(Key::PageDown) => scroll(ui, frame.page as isize),
        Event::Key(Key::Home) => scroll(ui, isize::MIN / 2),
        Event::Key(Key::End) => scroll(ui, isize::MAX / 2),
        Event::Wheel(by) => scroll(ui, by as isize),
        // a button's first letter chooses it
        Event::Key(Key::Char(ch)) => all
            .iter()
            .position(|(label, _)| {
                label.chars().next().map(|f| f.to_ascii_lowercase()) == Some(ch.to_ascii_lowercase())
            })
            .map(|i| {
                ui.focus = i;
                all[i].1
            }),
        Event::Click { col, row } => frame
            .hits
            .iter()
            .position(|h| h.row == row && (h.from..h.to).contains(&col))
            .map(|i| {
                ui.focus = i;
                frame.hits[i].action
            }),
    }
}

fn show(frame: &Frame, caps: Caps) {
    let mut s = frame.canvas.render(caps);
    if let Some((row, text)) = &frame.raw {
        s += &format!("\x1b[{};1H{text}", row + 1);
    }
    term::put(&s);
}

/// How a full-screen run ended.
pub enum Ended {
    Made(Box<Checked>),
    /// The check was asked for and could not finish: why.
    Failed(String),
    /// The person left without asking for the check.
    NotAsked,
}

/// Make the check on a thread of its own while this one keeps the screen alive: a mark that moves, the seconds, and
/// Ctrl-C, which a terminal held raw would otherwise swallow for as long as the check takes.
fn checking(
    raw: &Raw,
    caps: Caps,
    ui: &mut Ui,
    check: impl FnOnce() -> Result<Checked, String> + Send,
) -> Result<Checked, String> {
    std::thread::scope(|scope| {
        let work = scope.spawn(check);
        let mut pending = vec![];
        while !work.is_finished() {
            let (w, h) = term::size();
            show(&draw(ui, None, None, w, h, caps), caps);
            // a read that waits 0.2 s is the clock
            let stop = match term::events(&mut pending) {
                Some(evs) => evs.contains(&Event::Key(Key::Stop)),
                None => true,
            };
            if stop {
                // the check only reads, so there is nothing to undo: give the terminal back and go
                raw.restore();
                println!("{}: stopped. Nothing on this machine was changed.", guide::NAME);
                std::process::exit(130);
            }
            ui.ticks += 1;
        }
        work.join()
            .unwrap_or_else(|_| Err("it stopped unexpectedly (a bug in wend: please report it)".into()))
    })
}

/// Run the first run full-screen on a terminal already held. `check` makes the check when the person asks for it.
pub fn run(raw: Raw, caps: Caps, check: impl FnOnce() -> Result<Checked, String> + Send) -> Ended {
    let mut ui = Ui::new();
    let mut check = Some(check);
    let (mut made, mut error): (Option<Checked>, Option<String>) = (None, None);
    let mut pending = vec![];
    let mut drawn: Option<(usize, usize)> = None;
    loop {
        let (w, h) = term::size();
        // drawn again only when something changed: a key, a click, or the window's size
        let frame = draw(&ui, made.as_ref(), error.as_deref(), w, h, caps);
        if drawn != Some((w, h)) {
            // the mouse is given back on the screen with the link, so it can be selected
            raw.mouse(ui.screen != Screen::Share);
            show(&frame, caps);
            drawn = Some((w, h));
        }
        let Some(events) = term::events(&mut pending) else {
            break;
        };
        for ev in events {
            // a window too small has no buttons: q still leaves
            let small = frame.hits.is_empty();
            let action = match ev {
                Event::Key(Key::Char('q' | 'Q') | Key::Esc) if small => Some(Action::Quit),
                _ => step(&mut ui, ev, &frame),
            };
            drawn = None;
            match action {
                Some(Action::Quit) => {
                    drop(raw);
                    return match (made, error) {
                        (Some(c), _) => Ended::Made(Box::new(c)),
                        (None, Some(e)) => Ended::Failed(e),
                        (None, None) => Ended::NotAsked,
                    };
                }
                Some(Action::Check) => {
                    if let Some(f) = check.take() {
                        ui.go(Screen::Checking);
                        match checking(&raw, caps, &mut ui, f) {
                            Ok(c) => made = Some(c),
                            Err(e) => error = Some(e),
                        }
                        // keys pressed while it worked aren't meant for the screen that follows
                        term::discard_input();
                        pending.clear();
                    }
                    ui.go(if made.is_some() { Screen::Result } else { Screen::Failed });
                }
                Some(Action::Reads) => ui.go(Screen::Reads),
                Some(Action::Report) => ui.go(Screen::Report),
                Some(Action::Share) => ui.go(Screen::Share),
                Some(Action::Back) => {
                    let back = ui.back;
                    ui.go(back);
                }
                None => {}
            }
            // one thing asked for at a time: what was typed ahead of a new screen isn't meant for it
            if action.is_some() {
                break;
            }
        }
    }
    // the terminal went away
    drop(raw);
    match (made, error) {
        (Some(c), _) => Ended::Made(Box::new(c)),
        (None, Some(e)) => Ended::Failed(e),
        (None, None) => Ended::NotAsked,
    }
}

#[cfg(test)]
mod tests;
