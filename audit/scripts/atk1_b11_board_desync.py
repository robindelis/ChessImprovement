"""BUILDER-11 probe (frontend, real shipped code via tests/frontend_harness.js):
`removeBranch()` deletes the branch on the server but never touches the board,
the move history or the breadcrumbs, so the UI keeps showing a move that no
longer exists in the repertoire - and the "Mark Important" / note controls stay
enabled for it, which POSTs /api/repertoire/move_meta and silently RE-INSERTS
the deleted move (see BUILDER-02).
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import tests.test_setup  # noqa: F401,E402
import tests.frontend_runtime as frontend_runtime  # noqa: E402

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

SETUP_JS = """
const START = '%s';
const AFTER_E4 = '%s';
const b = window.repertoireBuilder;
b.activeColor = 'white';
b.currentStudy = 'My Line';
b.board = new window.InteractiveChessboard(document.getElementById('repertoireBoard'), {
    orientation: 'white', interactive: true,
    onMove: (from, to, promo, res) => b.onBoardMove(from, to, promo, res)
});
// The tree the server would have returned: 1.e4 is the prepared user move.
b.treeData = { moves_by_parent: {} };
b.treeData.moves_by_parent[START] = [
    { move_uci: 'e2e4', move_san: 'e4', is_user_move: 1, is_important: 0, comment: '', arrows: '' }
];
b.treeData.moves_by_parent[AFTER_E4] = [];
// Play 1.e4 through the REAL board + the REAL onBoardMove() so the board, the
// history timeline and the breadcrumbs all reflect the move that is about to be
// deleted from the repertoire.
b.history = [{ ply: 0, fen: START, san: 'Start', uci: null,
               openingName: 'White Opening Repertoire', eco: '' }];
b.currentStepIndex = 0;
{
    const res = b.board.playMove('e2', 'e4', undefined, false);
    b.onBoardMove('e2', 'e4', undefined, res);
}
b.renderMoveToolsPanel(b.history[b.currentStepIndex]);

// The Move Tools panel controls the harness DOM does not provide.
for (const id of ['btnRepToggleImportant', 'btnRepDeleteCurrentMove', 'repMoveComment',
                  'repArrowsCount', 'repMoveToolSan', 'repMoveToolType', 'repBreadcrumbs']) {
    if (!document.getElementById(id)) document.byId.set(id, document.createElement('div'));
}
b.renderMoveToolsPanel(b.history[b.currentStepIndex]);
b.renderBreadcrumbs();

// Server answers: the DELETE succeeded, the refreshed tree no longer has 1.e4.
let deleted = [];
window.fetch = (url, opts) => {
    const u = String(url);
    if ((opts && opts.method) === 'DELETE') { deleted.push(u); }
    b.treeData = { moves_by_parent: {} };   // post-delete tree: empty
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
        u.indexOf('/api/repertoire/tree') >= 0
            ? b.treeData
            : { status: 'ok', message: 'Branch deleted successfully.' }) });
};

const fill = document.createElement('div');  fill.id = 'repEvalBarFill';
const label = document.createElement('div'); label.id = 'repEvalBarLabel';
const realGet = document.getElementById.bind(document);
document.getElementById = (id) => id === 'repEvalBarFill' ? fill
                               : id === 'repEvalBarLabel' ? label : realGet(id);
""" % (START, AFTER_E4)


def run(body):
    p = frontend_runtime.run_scenario(SETUP_JS + "\n" + body)
    return {k: v for k, v in p.items() if k != "__harness"}


def main():
    print("=" * 78)
    print("state BEFORE deleting the move that is on the board")
    print("=" * 78)
    r = run("""
        const btn = document.getElementById('btnRepToggleImportant');
        const del = document.getElementById('btnRepDeleteCurrentMove');
        const note = document.getElementById('repMoveComment');
        return {
            boardFen: b.board.getFEN(),
            historySans: b.history.map(h => h.san),
            currentStep: b.history[b.currentStepIndex].san,
            deleteBtn: del ? { visibility: del.style.visibility, disabled: del.disabled } : 'missing',
            importantBtn: btn ? { visibility: btn.style.visibility, disabled: btn.disabled } : 'missing',
            noteDisabled: note ? note.disabled : 'missing',
            moveStillInTree: !!b.findMoveInTree(b.history[0].fen, 'e2e4')
        };
    """)
    print("  ", json.dumps(r, indent=2))

    print("\n" + "=" * 78)
    print("after b.removeBranch(START, 'e2e4')  -- the exact call the ✕ button makes")
    print("=" * 78)
    r = run("""
        return b.removeBranch(b.history[0].fen, 'e2e4').then(() => {
            const btn = document.getElementById('btnRepToggleImportant');
            const del = document.getElementById('btnRepDeleteCurrentMove');
            const step = b.history[b.currentStepIndex];
            return {
                deleteRequestsSent: deleted,
                serverTreeNowEmpty: Object.keys(b.treeData.moves_by_parent).length === 0,
                boardFen: b.board.getFEN(),
                boardStillShowsDeletedMove: b.board.getFEN() === step.fen,
                historySans: b.history.map(h => h.san),
                historyLength: b.history.length,
                currentStepSan: step.san,
                moveStillInTree: !!b.findMoveInTree(b.history[0].fen, 'e2e4'),
                deleteBtn: del ? { visibility: del.style.visibility, disabled: del.disabled } : 'missing',
                importantBtn: btn ? { visibility: btn.style.visibility, disabled: btn.disabled,
                                      label: btn.innerHTML } : 'missing',
                breadcrumbs: (document.getElementById('repBreadcrumbs') || {}).children
                    ? document.getElementById('repBreadcrumbs').children.map(c => c.textContent)
                    : null
            };
        });
    """)
    print("  ", json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
