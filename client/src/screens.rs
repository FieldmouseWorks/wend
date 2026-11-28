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
mod tests {
    use super::*;
    use crate::share::{Judged, Shared, System};
    use serde_json::{json, Value};

    const ASCII: Caps = Caps {
        colour: true,
        unicode: false,
    };
    const FULL: Caps = Caps {
        colour: true,
        unicode: true,
    };
    const LINK: &str = "https://github.com/FieldmouseWorks/wend/issues/new?template=box-report.yml&labels=box-report\
                        &title=box%3A%20CentOS%20Linux%20release%207.9.2009%20%28Core%29&system=CentOS%20Linux%20\
                        release%207.9.2009%20%28Core%29&route=centos7-to-alma10&details=%28drag%20wend-box-0123456789ab\
                        .md%20into%20this%20box%20to%20attach%20it%29";

    fn checked(verdict: Value) -> Checked {
        Checked {
            report: Shared {
                sys: System {
                    pretty: "CentOS Linux release 7.9.2009 (Core)".into(),
                    release: Some(("el".into(), "centos7".into())),
                },
                facts: json!({"arch": "x86_64", "ram_mib": 3936, "boot": "bios", "cpu": {"hypervisor": false}}),
                judged: vec![Judged {
                    name: "centos7-to-alma10".into(),
                    family: "el".into(),
                    joins: None,
                    hops: vec![],
                    verdict,
                }],
                summary: String::new(),
                full: (1..=60).map(|n| format!("report line {n}\n")).collect(),
                title: "box: CentOS Linux release 7.9.2009 (Core)".into(),
                routes: "centos7-to-alma10".into(),
                sys_line: "CentOS Linux release 7.9.2009 (Core)".into(),
                file: "wend-box-0123456789ab.md".into(),
            },
            paths: Some(vec![]),
            file: "/root/wend-x86_64-linux/wend-box-0123456789ab.md".into(),
            link: LINK.into(),
        }
    }

    fn yes() -> Value {
        json!({"ok": true, "from": "centos7", "to": "alma10", "lands_on": "alma10", "machine": [],
            "hops": [{"to": "alma9", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "alma10", "problems": [], "runtime_problems": [], "notes": []}]})
    }

    fn partway() -> Value {
        let problems: Vec<String> = (1..=12)
            .map(|n| format!("driver number {n} is not in this kernel"))
            .collect();
        json!({"ok": false, "from": "centos7", "to": "alma10", "lands_on": "alma9", "machine": [],
            "hops": [{"to": "alma9", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "alma10", "problems": problems, "runtime_problems": [], "notes": []}]})
    }

    fn at(screen: Screen) -> Ui {
        Ui {
            screen,
            back: Screen::Result,
            focus: 0,
            scroll: 0,
            ticks: 0,
        }
    }

    fn rows(f: &Frame) -> Vec<String> {
        (0..f.canvas.h).map(|r| f.canvas.row(r)).collect()
    }

    #[test]
    fn the_first_screen_is_a_frame_with_its_three_buttons() {
        let f = draw(&Ui::new(), None, None, 80, 24, ASCII);
        let r = rows(&f);
        assert!(r[0].starts_with("+- wend "), "{}", r[0]);
        assert!(
            r[0].ends_with(" proven upgrade paths for old Linux servers -+"),
            "{}",
            r[0]
        );
        assert_eq!(r[0].len(), 80);
        assert_eq!(
            r[2],
            "| Find out whether this machine can be upgraded.".to_string() + &" ".repeat(31) + "|"
        );
        assert!(
            r.iter()
                .any(|l| l.contains("    - It changes nothing on this machine.")),
            "{r:#?}"
        );
        // the frame is closed all the way down, and nothing spills past it
        for line in &r[1..22] {
            assert!(
                line.starts_with(['|', '+']) && line.ends_with(['|', '+']) && line.len() == 80,
                "{line:?}"
            );
        }
        assert_eq!(r[20], format!("+{}+", "-".repeat(78)));
        assert_eq!(
            r[21],
            format!("| {:<77}|", "[ Check this machine ]  [ What will it read? ]  [ Quit ]")
        );
        assert_eq!(r[22], format!("+{}+", "-".repeat(78)));
        assert!(
            r[23].contains("Enter: select") && r[23].contains("q: quit"),
            "{}",
            r[23]
        );
        // each button can be clicked exactly where it is drawn, and the first has the focus
        let want = [(2, 24, Action::Check), (26, 48, Action::Reads), (50, 58, Action::Quit)];
        assert_eq!(f.hits.len(), 3);
        for (h, (from, to, action)) in f.hits.iter().zip(want) {
            assert_eq!((h.row, h.from, h.to, h.action), (21, from, to, action));
            assert_eq!(&r[21][h.from..h.to].chars().next(), &Some('['));
        }
        assert_eq!(f.canvas.style_at(2, 21), Style::Focus);
        assert_eq!(f.canvas.style_at(26, 21), Style::Bold);
        assert_eq!((f.max_scroll, f.raw.is_none()), (0, true));
    }

    #[test]
    fn a_terminal_that_can_draws_lines_and_a_wide_one_centres_the_frame() {
        let f = draw(&Ui::new(), None, None, 80, 24, FULL);
        let r = rows(&f);
        assert!(
            r[0].starts_with("\u{250c}\u{2500} wend ") && r[0].ends_with("\u{2500}\u{2510}"),
            "{}",
            r[0]
        );
        assert!(r[1].starts_with('\u{2502}') && r[22].starts_with('\u{2514}'), "{r:#?}");
        assert!(
            r.iter()
                .any(|l| l.contains("    \u{2022} It changes nothing on this machine.")),
            "{r:#?}"
        );
        // every row is one screen wide in cells, whatever the characters
        assert!(r.iter().all(|l| l.chars().count() <= 80), "{r:#?}");
        let wide = draw(&Ui::new(), None, None, 200, 50, ASCII);
        let top = wide.canvas.row(0);
        assert_eq!((top.find('+'), top.len()), (Some(52), 52 + 96));
        assert_eq!(wide.hits[0].from, 54);
    }

    #[test]
    fn the_answer_is_set_apart_and_the_path_gets_real_arrows() {
        let made = checked(yes());
        let f = draw(&at(Screen::Result), Some(&made), None, 80, 24, FULL);
        let r = rows(&f);
        let row = |needle: &str| {
            r.iter()
                .position(|l| l.contains(needle))
                .unwrap_or_else(|| panic!("{needle}: {r:#?}"))
        };
        assert_eq!(
            r[row("THIS MACHINE") + 1],
            "\u{2502}   CentOS Linux release 7.9.2009 (Core)".to_string() + &" ".repeat(39) + "\u{2502}"
        );
        let answer = row("Yes, as far as this check can tell.");
        assert_eq!(f.canvas.style_at(4, answer), Style::Good);
        assert!(
            r[row("CentOS 7")].contains("CentOS 7 \u{2192} AlmaLinux 9 \u{2192} AlmaLinux 10"),
            "{r:#?}"
        );
        assert!(
            r[row("The report:")].contains("/root/wend-x86_64-linux/wend-box-0123456789ab.md"),
            "{r:#?}"
        );
        // on the smallest terminal a yes shows whole: nothing to scroll for
        assert_eq!(f.max_scroll, 0, "{r:#?}");
        let labels: Vec<Action> = f.hits.iter().map(|h| h.action).collect();
        assert_eq!(labels, [Action::Report, Action::Share, Action::Reads, Action::Quit]);
        // without the characters, the arrows it was written with
        let plain = rows(&draw(&at(Screen::Result), Some(&made), None, 80, 24, ASCII));
        assert!(
            plain
                .iter()
                .any(|l| l.contains("CentOS 7 -> AlmaLinux 9 -> AlmaLinux 10")),
            "{plain:#?}"
        );
        // a machine that stops partway is told so in the warning colour
        let made = checked(partway());
        let f = draw(&at(Screen::Result), Some(&made), None, 80, 24, ASCII);
        let r = rows(&f);
        let answer = r.iter().position(|l| l.contains("Part of the way.")).unwrap();
        assert_eq!(f.canvas.style_at(4, answer), Style::Warn);
    }

    #[test]
    fn text_longer_than_the_frame_scrolls_and_says_so() {
        let made = checked(partway());
        let mut ui = at(Screen::Result);
        let f = draw(&ui, Some(&made), None, 80, 24, ASCII);
        assert_eq!(f.page, 18);
        assert!(f.max_scroll > 0, "the twelve problems should not fit in 18 rows");
        let r = rows(&f);
        assert!(r[20].contains(" v more "), "{}", r[20]);
        assert!(!r.iter().any(|l| l.contains("The report:")), "{r:#?}");
        assert!(r[23].contains("Up/Down: scroll"), "{}", r[23]);
        // Down moves a line, End goes to the bottom and no further, and the marker moves to the top
        assert_eq!(step(&mut ui, Event::Key(Key::Down), &f), None);
        assert_eq!(ui.scroll, 1);
        step(&mut ui, Event::Key(Key::End), &f);
        assert_eq!(ui.scroll, f.max_scroll);
        step(&mut ui, Event::Key(Key::PageDown), &f);
        step(&mut ui, Event::Wheel(3), &f);
        assert_eq!(ui.scroll, f.max_scroll);
        let bottom = draw(&ui, Some(&made), None, 80, 24, ASCII);
        let r = rows(&bottom);
        assert!(r.iter().any(|l| l.contains("The report:")), "{r:#?}");
        assert!(r[1].contains(" ^ more ") && !r[20].contains("more"), "{r:#?}");
        step(&mut ui, Event::Wheel(-3), &bottom);
        assert_eq!(ui.scroll, f.max_scroll - 3);
        step(&mut ui, Event::Key(Key::Home), &bottom);
        step(&mut ui, Event::Key(Key::Up), &bottom);
        assert_eq!(ui.scroll, 0);
        // the report reads the same way: all sixty lines are reachable, a page at a time
        let mut ui = at(Screen::Report);
        let f = draw(&ui, Some(&made), None, 80, 24, ASCII);
        assert_eq!(f.max_scroll, 62 - 18);
        assert!(rows(&f)[4].contains("report line 1"), "{:#?}", rows(&f));
        step(&mut ui, Event::Key(Key::PageDown), &f);
        assert_eq!(ui.scroll, 18);
        assert!(rows(&draw(&ui, Some(&made), None, 80, 24, ASCII))[2].contains("report line 17"));
    }

    #[test]
    fn the_link_is_left_for_the_terminal_to_wrap_with_no_frame_beside_it() {
        let made = checked(yes());
        let f = draw(&at(Screen::Share), Some(&made), None, 80, 24, ASCII);
        let r = rows(&f);
        let (row, link) = f.raw.clone().expect("the link");
        assert_eq!(link, LINK);
        // it starts at the left edge under the text, and the rows it will wrap onto are empty and above the rule
        let need = LINK.len().div_ceil(80);
        assert!(r[row - 2].contains("the box marked Report, and submit:"), "{r:#?}");
        let file = r
            .iter()
            .position(|l| l.contains("2. Copy the file to a computer"))
            .expect("step 2");
        assert_eq!(
            r[file + 1].trim(),
            "/root/wend-x86_64-linux/wend-box-0123456789ab.md",
            "{r:#?}"
        );
        // all of it shows at once on the smallest terminal: nothing to scroll for on the way to the link
        assert_eq!(f.max_scroll, 0, "{r:#?}");
        assert!(
            row + need <= 20,
            "the link would run into the buttons: row {row}, {need} rows"
        );
        assert!(r[row..row + need].iter().all(String::is_empty), "{r:#?}");
        // no frame down the sides here: a selection across the wrapped link would take it along
        assert!(
            r[1..20].iter().all(|l| !l.starts_with('|') && !l.ends_with('|')),
            "{r:#?}"
        );
        assert!(r.iter().any(|l| l.contains("3. Open this link there")), "{r:#?}");
        assert_eq!(f.hits.iter().map(|h| h.action).collect::<Vec<_>>(), [Action::Back]);
        assert!(r[23].contains("Drag over the link to copy it."), "{}", r[23]);
    }

    #[test]
    fn a_window_too_small_says_so_instead_of_drawing_half_a_frame() {
        for (w, h) in [(79, 24), (80, 23), (40, 10)] {
            let f = draw(&Ui::new(), None, None, w, h, ASCII);
            assert!(f.hits.is_empty() && f.raw.is_none());
            assert_eq!(f.canvas.row(0), "wend needs a larger window:");
            assert_eq!(f.canvas.row(1), "at least 80 columns by 24 rows.");
            assert_eq!(f.canvas.row(4), "and run: wend --plain");
        }
    }

    #[test]
    fn the_help_under_the_frame_is_never_cut_short() {
        let made = checked(partway());
        for screen in [
            Screen::Welcome,
            Screen::Reads,
            Screen::Result,
            Screen::Report,
            Screen::Share,
            Screen::Failed,
        ] {
            for scrolls in [false, true] {
                assert!(
                    hint(screen, scrolls).len() <= 78,
                    "{screen:?}: {}",
                    hint(screen, scrolls)
                );
            }
            let f = draw(&at(screen), Some(&made), Some("why"), 80, 24, ASCII);
            let shown = f.canvas.row(23);
            assert_eq!(shown.trim_start(), hint(screen, f.max_scroll > 0), "{screen:?}");
        }
    }

    #[test]
    fn a_failed_check_says_why_and_that_nothing_changed() {
        let f = draw(
            &at(Screen::Failed),
            None,
            Some("/proc/1/maps: permission denied"),
            80,
            24,
            ASCII,
        );
        let r = rows(&f);
        assert!(r[2].contains("The check could not finish."), "{r:#?}");
        assert!(
            r.iter().any(|l| l.contains("/proc/1/maps: permission denied")),
            "{r:#?}"
        );
        assert!(
            r.iter().any(|l| l.contains("Nothing on this machine was changed.")),
            "{r:#?}"
        );
        assert_eq!(f.hits.iter().map(|h| h.action).collect::<Vec<_>>(), [Action::Quit]);
    }

    #[test]
    fn keys_and_clicks_choose_buttons() {
        let mut ui = Ui::new();
        let f = draw(&ui, None, None, 80, 24, ASCII);
        let key = |ui: &mut Ui, k| step(ui, Event::Key(k), &f);
        // Right and Tab go on, Left and Shift-Tab go back, both ways round
        assert_eq!((key(&mut ui, Key::Right), ui.focus), (None, 1));
        assert_eq!((key(&mut ui, Key::Tab), ui.focus), (None, 2));
        assert_eq!((key(&mut ui, Key::Tab), ui.focus), (None, 0));
        assert_eq!((key(&mut ui, Key::Left), ui.focus), (None, 2));
        assert_eq!((key(&mut ui, Key::BackTab), ui.focus), (None, 1));
        // Enter and Space take the focused one
        assert_eq!(key(&mut ui, Key::Enter), Some(Action::Reads));
        assert_eq!(key(&mut ui, Key::Char(' ')), Some(Action::Reads));
        // a button's first letter takes it, either case
        assert_eq!((key(&mut ui, Key::Char('c')), ui.focus), (Some(Action::Check), 0));
        assert_eq!((key(&mut ui, Key::Char('W')), ui.focus), (Some(Action::Reads), 1));
        assert_eq!(key(&mut ui, Key::Char('z')), None);
        // a click on a button takes it; a click beside one, or on its row's end, does nothing
        let click = |ui: &mut Ui, col, row| step(ui, Event::Click { col, row }, &f);
        assert_eq!((click(&mut ui, 2, 21), ui.focus), (Some(Action::Check), 0));
        assert_eq!((click(&mut ui, 57, 21), ui.focus), (Some(Action::Quit), 2));
        assert_eq!(click(&mut ui, 24, 21), None);
        assert_eq!(click(&mut ui, 58, 21), None);
        assert_eq!(click(&mut ui, 2, 20), None);
        assert_eq!(ui.focus, 2);
    }

    #[test]
    fn leaving_goes_back_one_screen_and_ctrl_c_always_quits() {
        let made = checked(yes());
        for (screen, leave) in [
            (Screen::Welcome, Action::Quit),
            (Screen::Result, Action::Quit),
            (Screen::Failed, Action::Quit),
            (Screen::Reads, Action::Back),
            (Screen::Report, Action::Back),
            (Screen::Share, Action::Back),
        ] {
            let mut ui = at(screen);
            let f = draw(&ui, Some(&made), None, 80, 24, ASCII);
            for k in [Key::Esc, Key::Char('q')] {
                assert_eq!(step(&mut ui, Event::Key(k), &f), Some(leave), "{screen:?} {k:?}");
            }
            assert_eq!(
                step(&mut ui, Event::Key(Key::Stop), &f),
                Some(Action::Quit),
                "{screen:?}"
            );
        }
        // while it checks there is nothing to choose; the screen shows it is alive and how to stop it
        let mut ui = at(Screen::Checking);
        ui.ticks = 37;
        let f = draw(&ui, None, None, 80, 24, ASCII);
        let r = rows(&f);
        assert!(f.hits.is_empty());
        assert!(r[2].contains("Checking this machine  /"), "{r:#?}");
        assert!(r.iter().any(|l| l.contains("7 s so far.")), "{r:#?}");
        assert_eq!(r[23].trim(), "Ctrl-C: stop");
        assert_eq!(step(&mut ui, Event::Key(Key::Enter), &f), None);
        assert_eq!(step(&mut ui, Event::Key(Key::Stop), &f), Some(Action::Quit));
    }

    #[test]
    fn back_returns_to_where_the_screen_was_opened_from() {
        let mut ui = Ui::new();
        ui.go(Screen::Reads);
        assert_eq!((ui.screen, ui.back), (Screen::Reads, Screen::Welcome));
        ui.focus = 3;
        ui.scroll = 7;
        ui.go(Screen::Welcome);
        assert_eq!((ui.screen, ui.focus, ui.scroll), (Screen::Welcome, 0, 0));
        ui.go(Screen::Checking);
        ui.go(Screen::Result);
        for sub in [Screen::Reads, Screen::Report, Screen::Share] {
            ui.go(sub);
            assert_eq!((ui.screen, ui.back), (sub, Screen::Result));
            ui.go(Screen::Result);
        }
    }
}
