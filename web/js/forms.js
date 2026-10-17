// A dirty form keeps local edits until save; signatures avoid resetting focus.
const forms = new Set();
export function createFormState() {
  const form = { dirty: false, signature: "",
    accepts(value, force = false) {
      const signature = JSON.stringify(value);
      if (!force && (this.dirty || this.signature === signature)) return false;
      this.signature = signature;
      return true;
    },
    reset() { this.dirty = false; this.signature = ""; }
  };
  forms.add(form);
  return form;
}
export function hasUnsavedForms() { return [...forms].some(form => form.dirty); }
export function resetForms() { for (const form of forms) form.reset(); }
