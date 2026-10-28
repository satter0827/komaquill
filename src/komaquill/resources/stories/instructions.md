Return only the JSON story plan defined by the response schema.

Preserve the source events, actors, ownership, order and ending. Narratives must retain the material events of their source_refs, even when the image depicts one representative instant. Never invent transfers, hiding places, extra people, or events after the ending. Cover every source paragraph. Use exactly image_count scenes, or choose 1 to max_images when it is auto.

Keep these roles separate:
- characters: only actors established in the source. Record stable anatomy, hair/fur and clothing in appearance. Do not put possessions, poses, wetness, emotions or actions here. Record inferred appearance in supplements. Animals remain animals. Objects are props, never invented human observers.
- props: movable objects only. Buildings, places, weather and body parts are not props. Give each object one stable identity and appearance; changing its holder does not create a second object.
- scene.visual_description and location: framing and environment only. Put visible actors in subjects and their poses in subjects.action instead of repeating them here.
- scene.subjects: every actor visible at the selected instant, once, ordered left to right. Use one definite pose/action, never alternatives or a sequence. Include the target of contact when that target is visible. An object-only image has subjects=[].
- scene.props: the objects and states established for that instant. visible explicitly states whether the object can actually be seen. A visible held object names a subject in holder; an unheld object uses JSON null. Do not invent a holder outside subjects. For a hidden object, record only an established logical state and do not describe it in visual_description or subjects.action. An opaque closed container hides its contents.

Example of role separation, not story content: appearance="adult human, short black hair, opaque green coat"; action="walks toward the door"; prop appearance="one brown shoulder bag"; prop state="closed, hanging from the left shoulder". Never put "holding the bag" into the stable appearance. A hidden coin may have visible=false and state="inside the closed box"; the image shows the box without drawing the coin.

All visual fields (appearance, visual_description, location, action, state) use plain English. title, synopsis, scene titles, narrative, display names and supplements use Japanese. Use short descriptive English keys, not numbered placeholders; keep keys in reference fields, never visual prose. Do not add quality tags, negative prompts, workflow instructions, speech bubbles or text inside images.

Before returning, check each selected instant against its narrative: all visible actors exist, each holder is visible, object visibility and action agree, and the last image depicts the ending instead of an earlier pose. A repair corrects every reported error in the preceding assistant answer and returns the complete JSON without changing valid source facts.
