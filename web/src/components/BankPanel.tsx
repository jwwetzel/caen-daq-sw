import type { BoardConfig, Catalog } from "../types";
import { SettingsList } from "./SettingsList";
import { Collapsible } from "./Collapsible";

interface Props {
  catalog: Catalog;
  config: BoardConfig;
  onGroupChange: (group: number, key: string, value: any) => void;
  locked?: (key: string) => boolean;
  onUnlock?: (key: string) => void;
}

/** Each bank collapses independently, enabled or not — the enable lives inside
 *  the bank it belongs to rather than floating above it. */
export function BankPanel({ catalog, config, onGroupChange,
                            locked, onUnlock }: Props) {
  const gsize = catalog.geometry.group_size;
  return (
    <div className="bank-settings">
      {config.groups.map((g, gi) => (
        <Collapsible
          key={gi}
          variant="nested"
          defaultOpen={g.enabled}
          title={`Bank ${gi}`}
          right={<span className="bank-ch">CH {gi * gsize}&ndash;{gi * gsize + gsize - 1}</span>}
        >
          <SettingsList
            defs={catalog.bank}
            geom={catalog.geometry}
            get={(k) => (g as any)[k]}
            onChange={(k, v) => onGroupChange(gi, k, v)}
            skip={["fast_trigger_threshold", "fast_trigger_dc_offset"]}
            locked={locked} onUnlock={onUnlock}
          />
        </Collapsible>
      ))}
    </div>
  );
}
