"""Debug MasterMob → SourceMob chain."""
import aaf2

with aaf2.open("C:/cisza test.aaf") as f:
    mob_by_id = {str(mob.mob_id): mob for mob in f.content.mobs}

    for mob in f.content.mobs:
        if type(mob).__name__ != "MasterMob":
            continue
        print(f"=== MasterMob: {mob.name} ===")
        for slot in mob.slots:
            sp = {p.name: p.value for p in slot.properties()}
            seg = slot.segment
            components = []
            if type(seg).__name__ == "Sequence":
                try:
                    components = list(seg.components)
                except Exception:
                    pass
            else:
                components = [seg]
            for comp in components:
                cp = {p.name: p.value for p in comp.properties()}
                ctype = type(comp).__name__
                slot_id = sp.get("SlotID")
                src_id = str(cp.get("SourceID", "?"))
                src_slot = cp.get("SourceMobSlotID", "?")
                src_start = cp.get("StartTime", "?")
                src_len = cp.get("Length", "?")
                edit_rate = sp.get("EditRate", "?")
                print(f"  slot={slot_id} rate={edit_rate} {ctype} -> mob={src_id[-20:]} srcSlot={src_slot} start={src_start} len={src_len}")
                # Resolve to SourceMob if possible
                if src_id in mob_by_id:
                    src_mob = mob_by_id[src_id]
                    print(f"    -> {type(src_mob).__name__}: {src_mob.name}")
                    desc = getattr(src_mob, "descriptor", None)
                    if desc:
                        print(f"       descriptor: {type(desc).__name__}")
                        try:
                            for loc in desc["Locator"].value:
                                for lp in loc.properties():
                                    if lp.name == "URLString":
                                        print(f"       URL: {lp.value}")
                        except Exception as e:
                            print(f"       (locator error: {e})")
