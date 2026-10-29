"""Render the user's reviewed English profile; never invent a translation."""
import html
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, HRFlowable


def build(job, candidate, target):
    profile=candidate.get("en")
    if not profile or not profile.get("summary") or not profile.get("experiences"):
        raise ValueError("Configure and review candidato.json > en before generating an English resume")
    styles=getSampleStyleSheet()
    body=ParagraphStyle("body",parent=styles["BodyText"],fontName="Helvetica",fontSize=9.3,leading=12,spaceAfter=5)
    heading=ParagraphStyle("section",parent=body,fontName="Helvetica-Bold",fontSize=10,spaceBefore=11,spaceAfter=6)
    name=ParagraphStyle("name",parent=body,fontName="Helvetica-Bold",fontSize=18,leading=22)
    def p(text,style=body):return Paragraph(html.escape(str(text)),style)
    job_text=(job["title"]+" "+job["description"]).lower()
    tracks={"technology":["cyber","security","technical support","it support","help desk"],"customer_service":["customer","support","service"],"administrative":["admin","assistant","backoffice"]}
    track=max(tracks,key=lambda key:sum(term in job_text for term in tracks[key]))
    summary=profile.get("summaries",{}).get(track,profile["summary"])
    experiences=profile["experiences"]
    by_id={e.get("id",str(i)):e for i,e in enumerate(experiences)}
    order=profile.get("experience_order",{}).get(track,list(by_id))
    story=[p(candidate["nome_completo"],name),p(f'{candidate["telefone"]} | {candidate["email"]} | {candidate.get("localidade","")}'),HRFlowable(width="100%",thickness=.5),p("PROFESSIONAL SUMMARY",heading),p(summary),p("PROFESSIONAL EXPERIENCE",heading)]
    for key in order:
        if key not in by_id:continue
        exp=by_id[key]
        story.extend([p(exp["company"],heading),p(exp["role"])])
        story.extend(p("• "+activity) for activity in exp.get("activities",[])[:3])
    for title,key in [("SKILLS","skills"),("EDUCATION","education"),("ADDITIONAL LEARNING","courses"),("LANGUAGES","languages")]:
        value=profile.get(key,[])
        if value:
            story.append(p(title,heading))
            story.append(p(" | ".join(value) if isinstance(value,list) else value))
            if key=="education":
                for extra in ["education_status","school"]:
                    if profile.get(extra):story.append(p(profile[extra]))
    target.parent.mkdir(parents=True,exist_ok=True)
    SimpleDocTemplate(str(target),pagesize=A4,leftMargin=14*mm,rightMargin=14*mm,topMargin=11*mm,bottomMargin=11*mm,title="Resume",author=candidate["nome_completo"]).build(story)
