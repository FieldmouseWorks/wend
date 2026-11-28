//! Tests of what the full-screen first run draws and what keys and clicks do, without a terminal.

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
        prog: "./wend".into(),
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
