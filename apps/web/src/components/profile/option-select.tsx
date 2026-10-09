"use client";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type Option<Value extends string> = { value: Value; label: string };

type OptionSelectProps<Value extends string> = {
  /** What is being chosen, for people who cannot see the surrounding row. */
  label: string;
  value: Value;
  options: readonly Option<Value>[];
  onChange: (value: Value) => void;
};

/** A labelled choice among a few fixed options of the profile schema. */
export function OptionSelect<Value extends string>({ label, value, options, onChange }: OptionSelectProps<Value>) {
  return (
    <Select
      items={options}
      value={value}
      onValueChange={(next) => {
        if (next !== null) onChange(next);
      }}
    >
      <SelectTrigger aria-label={label} className="min-w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
