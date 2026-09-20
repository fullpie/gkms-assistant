#pragma once
#include <stdinclude.hpp>

template <typename T = void*>
class CSListEditor {
public:
	CSListEditor(void* list, void* klass) {
		// list_klass = il2cpp_symbols::get_class_from_instance(list);
		list_klass = klass;
		lst = list;

		lst_get_Count_method = il2cpp_class_get_method_from_name(list_klass, "get_Count", 0);
		lst_get_Item_method = il2cpp_class_get_method_from_name(list_klass, "get_Item", 1);
		lst_Add_method = il2cpp_class_get_method_from_name(list_klass, "Add", 1);
		lst_ToArray_method = il2cpp_class_get_method_from_name(list_klass, "ToArray", 0);

		lst_get_Count = reinterpret_cast<lst_get_Count_t>(lst_get_Count_method->methodPointer);
		lst_get_Item = reinterpret_cast<lst_get_Item_t>(lst_get_Item_method->methodPointer);
		lst_Add = reinterpret_cast<lst_Add_t>(lst_Add_method->methodPointer);
		lst_ToArray = reinterpret_cast<lst_ToArray_t>(lst_ToArray_method->methodPointer);

	}

	void Add(T value) {
		lst_Add(lst, value, lst_Add_method);
	}

	T get_Item(int index) {
		return lst_get_Item(lst, index, lst_get_Item_method);
	}

	int get_Count() {
		return lst_get_Count(lst, lst_get_Count_method);
	}

	T operator[] (int key) {
		return get_Item(key);
	}

	Array<T>* ToArray() {
		return static_cast<Array<T>*>(lst_ToArray(lst, lst_ToArray_method));
	}

	class Iterator {
	public:
		Iterator(CSListEditor<T>* editor, int index) : editor(editor), index(index) {}

		T operator*() const {
			return editor->get_Item(index);
		}

		Iterator& operator++() {
			++index;
			return *this;
		}

		bool operator!=(const Iterator& other) const {
			return index != other.index;
		}

	private:
		CSListEditor<T>* editor;
		int index;
	};

	Iterator begin() {
		return Iterator(this, 0);
	}

	Iterator end() {
		return Iterator(this, get_Count());
	}

	void* lst;
	void* list_klass;
private:
	typedef T(*lst_get_Item_t)(void*, int, void* mtd);
	typedef void(*lst_Add_t)(void*, T, void* mtd);
	typedef int(*lst_get_Count_t)(void*, void* mtd);
	typedef void* (*lst_ToArray_t)(void*, void* mtd);

	MethodInfo* lst_get_Item_method;
	MethodInfo* lst_Add_method;
	MethodInfo* lst_ToArray_method;
	MethodInfo* lst_get_Count_method;

	lst_get_Item_t lst_get_Item;
	lst_Add_t lst_Add;
	lst_get_Count_t lst_get_Count;
	lst_ToArray_t lst_ToArray;
};
