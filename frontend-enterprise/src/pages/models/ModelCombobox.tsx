import { useEffect, useMemo, useRef, useState } from 'react';
import { LoaderCircle } from 'lucide-react';

export type ModelComboboxOption = { value: string; label: string };

type ModelComboboxProps = {
  value: string;
  onChange: (value: string) => void;
  options: ModelComboboxOption[];
  loading?: boolean;
  disabled?: boolean;
  placeholder?: string;
};

export default function ModelCombobox({
  value,
  onChange,
  options,
  loading = false,
  disabled = false,
  placeholder,
}: ModelComboboxProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  const filtered = useMemo(() => {
    const keyword = value.trim().toLowerCase();
    if (!keyword) return options;
    return options.filter(
      (option) => option.label.toLowerCase().includes(keyword) || option.value.toLowerCase().includes(keyword),
    );
  }, [options, value]);

  return (
    <div ref={containerRef} style={{ position: 'relative' }}>
      <input
        type="text"
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        autoComplete="off"
        data-1p-ignore="true"
        data-lpignore="true"
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        className="h-[36px] w-full rounded-[9px] border border-[#e3e7f1] px-[12px] text-[13px] text-[#18181a] outline-none focus:border-[#18181a] disabled:cursor-not-allowed disabled:bg-[#f9fafb] disabled:text-[#b7bccb]"
      />
      {loading && (
        <LoaderCircle className="absolute top-[11px] right-[12px] size-[14px] animate-spin text-[#858b9c]" />
      )}
      {open && !disabled && (
        <div className="absolute top-[calc(100%+4px)] left-0 z-10 max-h-[220px] w-full overflow-y-auto rounded-[10px] border border-[#e3e7f1] bg-white p-[4px] shadow-[0_8px_24px_rgba(20,20,30,0.12)]">
          {loading ? (
            <p className="px-[10px] py-[10px] text-center text-[12px] text-[#858b9c]">正在获取模型列表…</p>
          ) : filtered.length === 0 ? (
            <p className="px-[10px] py-[10px] text-center text-[12px] text-[#858b9c]">
              {options.length === 0 ? '未获取到模型列表，可直接手动输入' : '没有匹配的模型'}
            </p>
          ) : (
            filtered.map((option) => (
              <button
                key={option.value}
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
                className="block w-full rounded-[7px] px-[10px] py-[7px] text-left text-[13px] text-[#18181a] hover:bg-[#f6f6f7]"
              >
                {option.label}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
