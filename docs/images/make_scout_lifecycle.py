"""Generate the Scout candidate lifecycle diagram used by README.md.

Two SVGs are produced, one per colour scheme, because GitHub renders a README image
as a standalone <img>: CSS custom properties, ``currentColor`` and webfont links all
stop resolving there, and a transparent background is unreadable against one of the
two GitHub themes. So every colour is baked in literally, fonts fall back to generic
stacks, and each file paints its own background. README.md selects between them with
a <picture> element keyed on prefers-color-scheme.

Edit this script rather than the SVGs, so the two variants cannot drift apart:

    python docs/images/make_scout_lifecycle.py
"""

from pathlib import Path

THEMES = {
    'light': dict(bg='#FFFFFF', box='#F1F3F6', edge='#C8CFD8', ink='#1B1F26',
                  muted='#5A626E', accent='#A85C07', asoft='#F9EFE0', aedge='#C9924A'),
    'dark':  dict(bg='#0D1117', box='#1B2029', edge='#333B45', ink='#E6E9ED',
                  muted='#98A1AD', accent='#E0A254', asoft='#2A2114', aedge='#7E6033'),
}

SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"

TPL = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 500" width="720" height="500" role="img" aria-labelledby="t d">
<title id="t">Life of a Scout NEO candidate in a TOM</title>
<desc id="d">A candidate moves from the MPC NEOCP through the JPL Scout API and the rundataquery command into a Target with an active ScoutDetail. The updatescout command refreshes it from a single Scout roster call and appends each new orbit solution to ScoutDetailHistory. When the candidate is absent from the roster it is retired, and then resolves either to a renamed IAU designation or to no designation at all. Highlighted elements are added by the Rubin ToO filter fields branch.</desc>
<defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{ink}"/></marker>
<marker id="ara" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{accent}"/></marker>
</defs>

<rect x="0" y="0" width="720" height="500" fill="{bg}"/>

<rect x="270" y="16" width="180" height="48" rx="6" fill="{box}" stroke="{edge}"/>
<text x="360" y="37" text-anchor="middle" font-family="{sans}" font-size="13.5" font-weight="600" fill="{ink}">MPC NEOCP</text>
<text x="360" y="52" text-anchor="middle" font-family="{mono}" font-size="10" fill="{muted}">unconfirmed candidates</text>

<line x1="360" y1="64" x2="360" y2="104" stroke="{edge}" stroke-width="1.3" marker-end="url(#ar)"/>
<text x="372" y="88" font-family="{mono}" font-size="10" fill="{muted}">tracklet submitted</text>

<rect x="270" y="110" width="180" height="48" rx="6" fill="{box}" stroke="{edge}"/>
<text x="360" y="131" text-anchor="middle" font-family="{sans}" font-size="13.5" font-weight="600" fill="{ink}">JPL Scout API</text>
<text x="360" y="146" text-anchor="middle" font-family="{mono}" font-size="10" fill="{muted}">orbit + hazard scores</text>

<line x1="360" y1="158" x2="360" y2="198" stroke="{edge}" stroke-width="1.3" marker-end="url(#ar)"/>
<text x="348" y="182" text-anchor="end" font-family="{mono}" font-size="10" fill="{muted}">rundataquery &#183; score cuts</text>

<rect x="246" y="204" width="228" height="52" rx="6" fill="{box}" stroke="{edge}" stroke-width="1.4"/>
<text x="360" y="226" text-anchor="middle" font-family="{sans}" font-size="13.5" font-weight="600" fill="{ink}">Target + ScoutDetail</text>
<text x="360" y="242" text-anchor="middle" font-family="{mono}" font-size="10.5" fill="{muted}">active = true</text>

<path d="M 246 216 L 200 216 L 200 242 L 242 242" fill="none" stroke="{accent}" stroke-width="1.3" marker-end="url(#ara)"/>
<text x="194" y="205" text-anchor="end" font-family="{mono}" font-size="10" fill="{accent}">updatescout</text>
<text x="194" y="219" text-anchor="end" font-family="{mono}" font-size="10" fill="{muted}">refresh from</text>
<text x="194" y="232" text-anchor="end" font-family="{mono}" font-size="10" fill="{muted}">one roster call</text>

<text x="502" y="196" text-anchor="middle" font-family="{mono}" font-size="9.5" fill="{muted}">each new lastRun</text>
<line x1="476" y1="230" x2="526" y2="230" stroke="{accent}" stroke-width="1.3" marker-end="url(#ara)"/>
<rect x="530" y="204" width="174" height="52" rx="6" fill="{asoft}" stroke="{aedge}"/>
<text x="617" y="226" text-anchor="middle" font-family="{sans}" font-size="12.5" font-weight="600" fill="{ink}">ScoutDetailHistory</text>
<text x="617" y="242" text-anchor="middle" font-family="{mono}" font-size="10" fill="{muted}">append-only</text>

<line x1="360" y1="256" x2="360" y2="304" stroke="{accent}" stroke-width="1.3" marker-end="url(#ara)"/>
<text x="372" y="284" font-family="{mono}" font-size="10" fill="{muted}">absent from Scout roster</text>

<rect x="270" y="310" width="180" height="48" rx="6" fill="{asoft}" stroke="{aedge}"/>
<text x="360" y="331" text-anchor="middle" font-family="{sans}" font-size="13.5" font-weight="600" fill="{ink}">Retired</text>
<text x="360" y="346" text-anchor="middle" font-family="{mono}" font-size="10.5" fill="{muted}">active = false</text>

<path d="M 360 358 L 360 376" fill="none" stroke="{accent}" stroke-width="1.3"/>
<path d="M 173 376 L 547 376" fill="none" stroke="{accent}" stroke-width="1.3"/>
<line x1="173" y1="376" x2="173" y2="410" stroke="{accent}" stroke-width="1.3" marker-end="url(#ara)"/>
<line x1="547" y1="376" x2="547" y2="410" stroke="{accent}" stroke-width="1.3" marker-end="url(#ara)"/>
<text x="183" y="397" font-family="{mono}" font-size="9.5" fill="{muted}">on MPC Previous NEOCP</text>
<text x="537" y="397" text-anchor="end" font-family="{mono}" font-size="9.5" fill="{muted}">status = lost / dne / na / ns</text>

<rect x="28" y="416" width="290" height="58" rx="6" fill="{box}" stroke="{edge}"/>
<text x="173" y="439" text-anchor="middle" font-family="{sans}" font-size="12.5" font-weight="600" fill="{ink}">Renamed to IAU designation</text>
<text x="173" y="456" text-anchor="middle" font-family="{mono}" font-size="10" fill="{muted}">trksub kept as alias</text>

<rect x="402" y="416" width="290" height="58" rx="6" fill="{box}" stroke="{edge}"/>
<text x="547" y="439" text-anchor="middle" font-family="{sans}" font-size="12.5" font-weight="600" fill="{ink}">Never designated</text>
<text x="547" y="456" text-anchor="middle" font-family="{mono}" font-size="10" fill="{muted}">lost, bogus or artificial</text>

<text x="360" y="492" text-anchor="middle" font-family="{mono}" font-size="9.5" fill="{muted}">highlighted: added by the ScoutDetail lifecycle work</text>
</svg>
'''


def main():
    out_dir = Path(__file__).resolve().parent
    for name, colours in THEMES.items():
        target = out_dir / f'scout-lifecycle-{name}.svg'
        target.write_text(TPL.format(sans=SANS, mono=MONO, **colours))
        print(f'wrote {target.relative_to(out_dir.parent.parent)}')


if __name__ == '__main__':
    main()
