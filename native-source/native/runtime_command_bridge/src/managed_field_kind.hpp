#pragma once
namespace gkms::bridge {
// IL2CPP type kinds. Open generic, raw pointer, byref and function-pointer
// storage must never be interpreted as a managed object reference.
constexpr bool can_box_managed_field(int kind,bool byref){
    return !byref&&((kind>=2&&kind<=14)||kind==17||kind==18||kind==20||kind==21||kind==24||kind==25||kind==28||kind==29);
}
constexpr bool managed_reference_element(int kind,bool byref,bool value_type){
    return !byref&&!value_type&&(kind==14||kind==18||kind==20||kind==21||kind==28||kind==29);
}
}
