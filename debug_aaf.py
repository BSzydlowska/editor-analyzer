"""
Diagnostic script — dumps all mob/locator/descriptor info from an AAF file.
Run with:  uv run python debug_aaf.py <path_to.aaf>
"""
import sys
import aaf2

def dump_aaf(path: str) -> None:
    with aaf2.open(path) as f:
        for mob in f.content.mobs:
            mob_type = type(mob).__name__
            print(f"\n{'='*60}")
            print(f"MOB: {mob_type}  name={getattr(mob, 'name', '?')}")
            print(f"  mob_id={getattr(mob, 'mob_id', '?')}")

            # Try descriptor
            desc = getattr(mob, "descriptor", None)
            if desc is None:
                print("  descriptor: NONE")
                continue

            print(f"  descriptor type: {type(desc).__name__}")

            # Print all properties on descriptor
            try:
                for prop in desc.properties():
                    print(f"    prop: {prop.name!r} = {prop.value!r}")
            except Exception as e:
                print(f"    (error iterating props: {e})")

            # Try locators via 'Locator' property (pyaaf2 actual API)
            print("  --- Locators ---")
            try:
                loc_prop = desc['Locator']
                locs = list(loc_prop.value)
                if not locs:
                    print("  (no locators)")
                for loc in locs:
                    print(f"    locator type: {type(loc).__name__}")
                    try:
                        for prop in loc.properties():
                            print(f"      prop: {prop.name!r} = {prop.value!r}")
                    except Exception as e:
                        print(f"      (error: {e})")
            except (KeyError, AttributeError) as e:
                print(f"  (error accessing locators via ['Locator']: {e})")
            # Also try old way for comparison
            try:
                list(desc.locators)
            except AttributeError as e:
                print(f"  (confirmed: desc.locators raises: {e})")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python debug_aaf.py <file.aaf>")
        sys.exit(1)
    dump_aaf(sys.argv[1])
