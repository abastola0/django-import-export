from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from unittest import mock

import tablib
from django.contrib.auth.models import User
from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    ValidationError,
)
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count
from django.db.utils import ConnectionDoesNotExist
from django.test import TestCase, skipUnlessDBFeature

from import_export import fields, resources, results, widgets
from import_export.instance_loaders import ModelInstanceLoader
from import_export.resources import Diff

from ....models import (
    Author,
    Book,
    Category,
    Entry,
    Profile,
    WithDefault,
    WithDynamicDefault,
    WithFloatField,
)


class CategoryResource(resources.ModelResource):
    class Meta:
        model = Category


class HarshRussianWidget(widgets.CharWidget):
    def clean(self, value, row=None, *args, **kwargs):
        raise ValueError("Ова вриједност је страшна!")


class AuthorResourceWithCustomWidget(resources.ModelResource):
    class Meta:
        model = Author

    @classmethod
    def widget_from_django_field(cls, f, default=widgets.Widget):
        if f.name == "name":
            return HarshRussianWidget
        result = default
        internal_type = (
            f.get_internal_type()
            if callable(getattr(f, "get_internal_type", None))
            else ""
        )
        if internal_type in cls.WIDGETS_MAP:
            result = cls.WIDGETS_MAP[internal_type]
            if isinstance(result, str):
                result = getattr(cls, result)(f)
        return result


class ProfileResource(resources.ModelResource):
    class Meta:
        model = Profile
        exclude = ("user",)


class WithDefaultResource(resources.ModelResource):
    class Meta:
        model = WithDefault
        fields = ("name",)


class BookResource(resources.ModelResource):
    published = fields.Field(column_name="published_date")

    class Meta:
        model = Book
        exclude = ("imported",)


class BookResourceWithStoreInstance(resources.ModelResource):
    class Meta:
        model = Book
        store_instance = True


class BookResourceWithLineNumberLogger(BookResource):
    def __init__(self, *args, **kwargs):
        self.before_lines = []
        self.after_lines = []
        return super().__init__(*args, **kwargs)

    def before_import_row(self, row, row_number=None, **kwargs):
        self.before_lines.append(row_number)

    def after_import_row(self, row, row_result, row_number=None, **kwargs):
        self.after_lines.append(row_number)


class AuthorResource(resources.ModelResource):
    books = fields.Field(
        column_name="books",
        attribute="book_set",
        readonly=True,
    )

    class Meta:
        model = Author
        export_order = ("name", "books")


class ModelResourceTest(TestCase):
    def setUp(self):
        self.resource = BookResource()

        self.book = Book.objects.create(name="Some book")
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email", "price"])
        row = [self.book.pk, "Some book", "test@example.com", "10.25"]
        self.dataset.append(row)

    def test_default_instance_loader_class(self):
        self.assertIs(self.resource._meta.instance_loader_class, ModelInstanceLoader)

    def test_fields(self):
        fields = self.resource.fields
        self.assertIn("id", fields)
        self.assertIn("name", fields)
        self.assertIn("author_email", fields)
        self.assertIn("price", fields)

    def test_fields_foreign_key(self):
        fields = self.resource.fields
        self.assertIn("author", fields)
        widget = fields["author"].widget
        self.assertIsInstance(widget, widgets.ForeignKeyWidget)
        self.assertEqual(widget.model, Author)

    def test_get_display_name(self):
        display_name = self.resource.get_display_name()
        self.assertEqual(display_name, "BookResource")

        class BookResource(resources.ModelResource):
            class Meta:
                name = "Foo Name"
                model = Book
                import_id_fields = ["name"]

        resource = BookResource()
        display_name = resource.get_display_name()
        self.assertEqual(display_name, "Foo Name")

    def test_fields_m2m(self):
        fields = self.resource.fields
        self.assertIn("categories", fields)

    def test_excluded_fields(self):
        self.assertNotIn("imported", self.resource.fields)

    def test_init_instance(self):
        instance = self.resource.init_instance()
        self.assertIsInstance(instance, Book)

    def test_default(self):
        self.assertEqual(
            WithDefaultResource.fields["name"].clean({"name": ""}), "foo_bar"
        )

    def test_get_instance(self):
        instance_loader = self.resource._meta.instance_loader_class(self.resource)
        self.resource._meta.import_id_fields = ["id"]
        instance = self.resource.get_instance(instance_loader, self.dataset.dict[0])
        self.assertEqual(instance, self.book)

    def test_get_instance_import_id_fields(self):
        class BookResource(resources.ModelResource):
            name = fields.Field(attribute="name", widget=widgets.CharWidget())

            class Meta:
                model = Book
                import_id_fields = ["name"]

        resource = BookResource()
        instance_loader = resource._meta.instance_loader_class(resource)
        instance = resource.get_instance(instance_loader, self.dataset.dict[0])
        self.assertEqual(instance, self.book)

    def test_get_instance_import_id_fields_with_custom_column_name(self):
        class BookResource(resources.ModelResource):
            name = fields.Field(
                attribute="name", column_name="book_name", widget=widgets.CharWidget()
            )

            class Meta:
                model = Book
                import_id_fields = ["name"]

        dataset = tablib.Dataset(headers=["id", "book_name", "author_email", "price"])
        row = [self.book.pk, "Some book", "test@example.com", "10.25"]
        dataset.append(row)

        resource = BookResource()
        instance_loader = resource._meta.instance_loader_class(resource)
        instance = resource.get_instance(instance_loader, dataset.dict[0])
        self.assertEqual(instance, self.book)

    def test_get_instance_usually_defers_to_instance_loader(self):
        self.resource._meta.import_id_fields = ["id"]

        instance_loader = self.resource._meta.instance_loader_class(self.resource)

        with mock.patch.object(instance_loader, "get_instance") as mocked_method:
            row = self.dataset.dict[0]
            self.resource.get_instance(instance_loader, row)
            # instance_loader.get_instance() should have been called
            mocked_method.assert_called_once_with(row)

    def test_get_instance_when_id_fields_not_in_dataset(self):
        self.resource._meta.import_id_fields = ["id"]

        # construct a dataset with a missing "id" column
        dataset = tablib.Dataset(headers=["name", "author_email", "price"])
        dataset.append(["Some book", "test@example.com", "10.25"])

        instance_loader = self.resource._meta.instance_loader_class(self.resource)

        with mock.patch.object(instance_loader, "get_instance") as mocked_method:
            result = self.resource.get_instance(instance_loader, dataset.dict[0])
            # Resource.get_instance() should return None
            self.assertIs(result, None)
            # instance_loader.get_instance() should NOT have been called
            mocked_method.assert_not_called()

    def test_get_export_headers(self):
        headers = self.resource.get_export_headers()
        self.assertEqual(
            headers,
            [
                "published_date",
                "id",
                "name",
                "author",
                "author_email",
                "published_time",
                "price",
                "added",
                "categories",
            ],
        )

    def test_export(self):
        with self.assertNumQueries(2):
            dataset = self.resource.export(Book.objects.all())
            self.assertEqual(len(dataset), 1)

    def test_export_iterable(self):
        with self.assertNumQueries(2):
            dataset = self.resource.export(list(Book.objects.all()))
            self.assertEqual(len(dataset), 1)

    def test_export_prefetch_related(self):
        with self.assertNumQueries(3):
            dataset = self.resource.export(
                Book.objects.prefetch_related("categories").all()
            )
            self.assertEqual(len(dataset), 1)

    def test_export_handles_args(self):
        # issue 1565
        with self.assertWarns(DeprecationWarning) as w:
            self.resource.export(Book.objects.none())
            self.assertEqual(
                "'queryset' must be supplied as a named parameter",
                str(w.warnings[0].message),
            )

    def test_export_handles_named_queryset_parameter(self):
        class _BookResource(BookResource):
            def before_export(self, queryset, *args, **kwargs):
                self.qs = queryset
                self.args_ = args
                self.kwargs_ = kwargs

        self.resource = _BookResource()
        # when queryset is supplied, it should be passed to before_export()
        self.resource.export(1, 2, 3, queryset=Book.objects.all(), **{"a": 1})
        self.assertEqual(Book.objects.count(), len(self.resource.qs))
        self.assertEqual((1, 2, 3), self.resource.args_)
        self.assertEqual(dict(a=1), self.resource.kwargs_)

    def test_iter_queryset(self):
        qs = Book.objects.all()
        with mock.patch.object(qs, "iterator") as mocked_method:
            list(self.resource.iter_queryset(qs))
            mocked_method.assert_called_once_with(chunk_size=100)

    def test_iter_queryset_prefetch_unordered(self):
        qsu = Book.objects.prefetch_related("categories").all()
        qso = qsu.order_by("pk").all()
        with mock.patch.object(qsu, "order_by") as mocked_method:
            mocked_method.return_value = qso
            list(self.resource.iter_queryset(qsu))
            mocked_method.assert_called_once_with("pk")

    def test_iter_queryset_prefetch_ordered(self):
        qs = Book.objects.prefetch_related("categories").order_by("pk").all()
        with mock.patch("import_export.resources.Paginator", autospec=True) as p:
            p.return_value = Paginator(qs, 100)
            list(self.resource.iter_queryset(qs))
            p.assert_called_once_with(qs, 100)

    def test_iter_queryset_prefetch_chunk_size(self):
        class B(BookResource):
            class Meta:
                chunk_size = 1000

        paginator = "import_export.resources.Paginator"
        qs = Book.objects.prefetch_related("categories").order_by("pk").all()
        with mock.patch(paginator, autospec=True) as mocked_obj:
            mocked_obj.return_value = Paginator(qs, 1000)
            list(B().iter_queryset(qs))
            mocked_obj.assert_called_once_with(qs, 1000)

    def test_get_diff(self):
        diff = Diff(self.resource, self.book, False)
        book2 = Book(name="Some other book")
        diff.compare_with(self.resource, book2)
        html = diff.as_html()
        headers = self.resource.get_export_headers()
        self.assertEqual(
            html[headers.index("name")],
            '<span>Some </span><ins style="background:#e6ffe6;">'
            "other </ins><span>book</span>",
        )
        self.assertFalse(html[headers.index("author_email")])

    def test_import_data_update(self):
        result = self.resource.import_data(self.dataset, raise_errors=True)

        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 1)
        self.assertTrue(result.rows[0].diff)
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_UPDATE
        )
        self.assertEqual(result.rows[0].row_values.get("name"), None)
        self.assertEqual(result.rows[0].row_values.get("author_email"), None)

        self.assertIsNone(result.rows[0].instance)
        self.assertIsNotNone(result.rows[0].original)

        instance = Book.objects.get(pk=self.book.pk)
        self.assertEqual(instance.author_email, "test@example.com")
        self.assertEqual(instance.price, Decimal("10.25"))

    def test_import_data_new(self):
        Book.objects.all().delete()
        self.assertEqual(0, Book.objects.count())
        result = self.resource.import_data(self.dataset, raise_errors=True)

        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 1)
        self.assertTrue(result.rows[0].diff)
        self.assertEqual(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_NEW)
        self.assertEqual(result.rows[0].row_values.get("name"), None)
        self.assertEqual(result.rows[0].row_values.get("author_email"), None)

        self.assertIsNone(result.rows[0].instance)
        self.assertIsNone(result.rows[0].original)

        self.assertEqual(1, Book.objects.count())
        instance = Book.objects.first()
        self.assertEqual(instance.author_email, "test@example.com")
        self.assertEqual(instance.price, Decimal("10.25"))

    def test_import_data_new_store_instance(self):
        self.resource = BookResourceWithStoreInstance()
        Book.objects.all().delete()
        self.assertEqual(0, Book.objects.count())
        result = self.resource.import_data(self.dataset, raise_errors=True)

        self.assertEqual(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_NEW)
        self.assertIsNotNone(result.rows[0].instance)
        self.assertIsNone(result.rows[0].original)
        self.assertEqual(1, Book.objects.count())
        book = Book.objects.first()
        self.assertEqual(book.pk, result.rows[0].instance.pk)

    def test_import_data_update_store_instance(self):
        self.resource = BookResourceWithStoreInstance()
        result = self.resource.import_data(self.dataset, raise_errors=True)
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_UPDATE
        )
        self.assertIsNotNone(result.rows[0].instance)
        self.assertIsNotNone(result.rows[0].original)
        self.assertEqual(1, Book.objects.count())
        book = Book.objects.first()
        self.assertEqual(book.pk, result.rows[0].instance.pk)

    @skipUnlessDBFeature("supports_transactions")
    @mock.patch("import_export.resources.connections")
    def test_import_data_no_transaction(self, mock_db_connections):
        class Features:
            supports_transactions = False

        class DummyConnection:
            features = Features()

        dummy_connection = DummyConnection()
        mock_db_connections.__getitem__.return_value = dummy_connection
        result = self.resource.import_data(
            self.dataset, dry_run=True, use_transactions=False, raise_errors=True
        )

        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 1)
        self.assertTrue(result.rows[0].diff)
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_UPDATE
        )
        self.assertEqual(result.rows[0].row_values.get("name"), None)
        self.assertEqual(result.rows[0].row_values.get("author_email"), None)

    @mock.patch("import_export.resources.connections")
    def test_ImproperlyConfigured_if_use_transactions_set_when_not_supported(
        self, mock_db_connections
    ):
        class Features(object):
            supports_transactions = False

        class DummyConnection(object):
            features = Features()

        dummy_connection = DummyConnection()
        mock_db_connections.__getitem__.return_value = dummy_connection
        with self.assertRaises(ImproperlyConfigured):
            self.resource.import_data(
                self.dataset,
                use_transactions=True,
            )

    def test_importing_with_line_number_logging(self):
        resource = BookResourceWithLineNumberLogger()
        resource.import_data(self.dataset, raise_errors=True)
        self.assertEqual(resource.before_lines, [1])
        self.assertEqual(resource.after_lines, [1])

    def test_import_data_raises_field_specific_validation_errors(self):
        resource = AuthorResource()
        dataset = tablib.Dataset(headers=["id", "name", "birthday"])
        dataset.append(["", "A.A.Milne", "1882test-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        self.assertIn("birthday", result.invalid_rows[0].field_specific_errors)

    def test_import_data_raises_field_specific_validation_errors_with_skip_unchanged(
        self,
    ):
        resource = AuthorResource()
        resource._meta.skip_unchanged = True

        author = Author.objects.create(name="Some author")

        dataset = tablib.Dataset(headers=["id", "birthday"])
        dataset.append([author.id, "1882test-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        self.assertIn("birthday", result.invalid_rows[0].field_specific_errors)

    def test_import_data_empty_dataset_with_collect_failed_rows(self):
        resource = AuthorResource()
        result = resource.import_data(tablib.Dataset(), collect_failed_rows=True)
        self.assertEqual(["Error"], result.failed_dataset.headers)

    def test_collect_failed_rows(self):
        resource = ProfileResource()
        headers = ["id", "user"]
        # 'user' is a required field, the database will raise an error.
        row = [None, None]
        dataset = tablib.Dataset(row, headers=headers)
        result = resource.import_data(
            dataset,
            dry_run=True,
            use_transactions=True,
            collect_failed_rows=True,
        )
        self.assertEqual(result.failed_dataset.headers, ["id", "user", "Error"])
        self.assertEqual(len(result.failed_dataset), 1)
        # We can't check the error message because it's package- and version-dependent

    def test_row_result_raise_errors(self):
        resource = ProfileResource()
        headers = ["id", "user"]
        # 'user' is a required field, the database will raise an error.
        row = [None, None]
        dataset = tablib.Dataset(row, headers=headers)
        with self.assertRaises(IntegrityError):
            resource.import_data(
                dataset,
                dry_run=True,
                use_transactions=True,
                raise_errors=True,
            )

    def test_collect_failed_rows_validation_error(self):
        resource = ProfileResource()
        row = ["1"]
        dataset = tablib.Dataset(row, headers=["id"])
        with mock.patch(
            "import_export.resources.Field.save", side_effect=ValidationError("fail!")
        ):
            result = resource.import_data(
                dataset,
                dry_run=True,
                use_transactions=True,
                collect_failed_rows=True,
            )
        self.assertEqual(result.failed_dataset.headers, ["id", "Error"])
        self.assertEqual(
            1,
            len(result.failed_dataset),
        )
        self.assertEqual("1", result.failed_dataset.dict[0]["id"])
        self.assertEqual(
            "{'__all__': ['fail!']}", result.failed_dataset.dict[0]["Error"]
        )

    def test_row_result_raise_ValidationError(self):
        resource = ProfileResource()
        row = ["1"]
        dataset = tablib.Dataset(row, headers=["id"])
        with mock.patch(
            "import_export.resources.Field.save", side_effect=ValidationError("fail!")
        ):
            with self.assertRaisesRegex(ValidationError, "{'__all__': \\['fail!'\\]}"):
                resource.import_data(
                    dataset,
                    dry_run=True,
                    use_transactions=True,
                    raise_errors=True,
                )

    def test_import_data_handles_widget_valueerrors_with_unicode_messages(self):
        resource = AuthorResourceWithCustomWidget()
        dataset = tablib.Dataset(headers=["id", "name", "birthday"])
        dataset.append(["", "A.A.Milne", "1882-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        self.assertEqual(
            result.invalid_rows[0].field_specific_errors["name"],
            ["Ова вриједност је страшна!"],
        )

    def test_model_validation_errors_not_raised_when_clean_model_instances_is_false(
        self,
    ):
        class TestResource(resources.ModelResource):
            class Meta:
                model = Author
                clean_model_instances = False

        resource = TestResource()
        dataset = tablib.Dataset(headers=["id", "name"])
        dataset.append(["", "123"])

        result = resource.import_data(dataset, raise_errors=False)
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(len(result.invalid_rows), 0)

    def test_model_validation_errors_raised_when_clean_model_instances_is_true(self):
        class TestResource(resources.ModelResource):
            class Meta:
                model = Author
                clean_model_instances = True
                export_order = ["id", "name", "birthday"]

        # create test dataset
        # NOTE: column order is deliberately strange
        dataset = tablib.Dataset(headers=["name", "id"])
        dataset.append(["123", "1"])

        # run import_data()
        resource = TestResource()
        result = resource.import_data(dataset, raise_errors=False)

        # check has_validation_errors()
        self.assertTrue(result.has_validation_errors())

        # check the invalid row itself
        invalid_row = result.invalid_rows[0]
        self.assertEqual(invalid_row.error_count, 1)
        self.assertEqual(
            invalid_row.field_specific_errors, {"name": ["'123' is not a valid value"]}
        )
        # diff_header and invalid_row.values should match too
        self.assertEqual(result.diff_headers, ["id", "name", "birthday"])
        self.assertEqual(invalid_row.values, ("1", "123", "---"))

    def test_known_invalid_fields_are_excluded_from_model_instance_cleaning(self):
        # The custom widget on the parent class should complain about
        # 'name' first, preventing Author.full_clean() from raising the
        # error as it does in the previous test

        class TestResource(AuthorResourceWithCustomWidget):
            class Meta:
                model = Author
                clean_model_instances = True

        resource = TestResource()
        dataset = tablib.Dataset(headers=["id", "name"])
        dataset.append(["", "123"])

        result = resource.import_data(dataset, raise_errors=False)
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(result.invalid_rows[0].error_count, 1)
        self.assertEqual(
            result.invalid_rows[0].field_specific_errors,
            {"name": ["Ова вриједност је страшна!"]},
        )

    def test_import_data_error_saving_model(self):
        row = list(self.dataset.pop())
        # set pk to something that would yield error
        row[0] = "foo"
        self.dataset.append(row)
        result = self.resource.import_data(self.dataset, raise_errors=False)

        self.assertTrue(result.has_errors())
        self.assertTrue(result.rows[0].errors)
        actual = result.rows[0].errors[0].error
        self.assertIsInstance(actual, (ValueError, InvalidOperation))
        self.assertIn(
            str(actual),
            {
                "could not convert string to float",
                "[<class 'decimal.ConversionSyntax'>]",
            },
        )

    def test_import_data_delete(self):
        class B(BookResource):
            delete = fields.Field(widget=widgets.BooleanWidget())

            def for_delete(self, row, instance):
                return self.fields["delete"].clean(row)

        row = [self.book.pk, self.book.name, "1"]
        dataset = tablib.Dataset(*[row], headers=["id", "name", "delete"])
        result = B().import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_DELETE
        )
        self.assertFalse(Book.objects.filter(pk=self.book.pk))
        self.assertIsNone(result.rows[0].instance)
        self.assertIsNone(result.rows[0].original)

    def test_import_data_delete_store_instance(self):
        class B(BookResource):
            delete = fields.Field(widget=widgets.BooleanWidget())

            def for_delete(self, row, instance):
                return self.fields["delete"].clean(row)

            class Meta:
                store_instance = True

        row = [self.book.pk, self.book.name, "1"]
        dataset = tablib.Dataset(*[row], headers=["id", "name", "delete"])
        result = B().import_data(dataset, raise_errors=True)
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_DELETE
        )
        self.assertIsNotNone(result.rows[0].instance)

    def test_save_instance_with_dry_run_flag(self):
        class B(BookResource):
            def before_save_instance(self, instance, using_transactions, dry_run):
                super().before_save_instance(instance, using_transactions, dry_run)
                if dry_run:
                    self.before_save_instance_dry_run = True
                else:
                    self.before_save_instance_dry_run = False

            def save_instance(
                self, instance, new, using_transactions=True, dry_run=False
            ):
                super().save_instance(instance, new, using_transactions, dry_run)
                if dry_run:
                    self.save_instance_dry_run = True
                else:
                    self.save_instance_dry_run = False

            def after_save_instance(self, instance, using_transactions, dry_run):
                super().after_save_instance(instance, using_transactions, dry_run)
                if dry_run:
                    self.after_save_instance_dry_run = True
                else:
                    self.after_save_instance_dry_run = False

        resource = B()
        resource.import_data(self.dataset, dry_run=True, raise_errors=True)
        self.assertTrue(resource.before_save_instance_dry_run)
        self.assertTrue(resource.save_instance_dry_run)
        self.assertTrue(resource.after_save_instance_dry_run)

        resource.import_data(self.dataset, dry_run=False, raise_errors=True)
        self.assertFalse(resource.before_save_instance_dry_run)
        self.assertFalse(resource.save_instance_dry_run)
        self.assertFalse(resource.after_save_instance_dry_run)

    @mock.patch("core.models.Book.save")
    def test_save_instance_noop(self, mock_book):
        book = Book.objects.first()
        self.resource.save_instance(
            book, is_create=False, using_transactions=False, dry_run=True
        )
        self.assertEqual(0, mock_book.call_count)

    @mock.patch("core.models.Book.save")
    def test_delete_instance_noop(self, mock_book):
        book = Book.objects.first()
        self.resource.delete_instance(book, using_transactions=False, dry_run=True)
        self.assertEqual(0, mock_book.call_count)

    def test_delete_instance_with_dry_run_flag(self):
        class B(BookResource):
            delete = fields.Field(widget=widgets.BooleanWidget())

            def for_delete(self, row, instance):
                return self.fields["delete"].clean(row)

            def before_delete_instance(self, instance, dry_run):
                super().before_delete_instance(instance, dry_run)
                if dry_run:
                    self.before_delete_instance_dry_run = True
                else:
                    self.before_delete_instance_dry_run = False

            def delete_instance(self, instance, using_transactions=True, dry_run=False):
                super().delete_instance(instance, using_transactions, dry_run)
                if dry_run:
                    self.delete_instance_dry_run = True
                else:
                    self.delete_instance_dry_run = False

            def after_delete_instance(self, instance, dry_run):
                super().after_delete_instance(instance, dry_run)
                if dry_run:
                    self.after_delete_instance_dry_run = True
                else:
                    self.after_delete_instance_dry_run = False

        resource = B()
        row = [self.book.pk, self.book.name, "1"]
        dataset = tablib.Dataset(*[row], headers=["id", "name", "delete"])
        resource.import_data(dataset, dry_run=True, raise_errors=True)
        self.assertTrue(resource.before_delete_instance_dry_run)
        self.assertTrue(resource.delete_instance_dry_run)
        self.assertTrue(resource.after_delete_instance_dry_run)

        resource.import_data(dataset, dry_run=False, raise_errors=True)
        self.assertFalse(resource.before_delete_instance_dry_run)
        self.assertFalse(resource.delete_instance_dry_run)
        self.assertFalse(resource.after_delete_instance_dry_run)

    def test_relationships_fields(self):
        class B(resources.ModelResource):
            class Meta:
                model = Book
                fields = ("author__name",)

        author = Author.objects.create(name="Author")
        self.book.author = author
        resource = B()
        result = resource.fields["author__name"].export(self.book)
        self.assertEqual(result, author.name)

    def test_dehydrating_fields(self):
        class B(resources.ModelResource):
            full_title = fields.Field(column_name="Full title")

            class Meta:
                model = Book
                fields = ("author__name", "full_title")

            def dehydrate_full_title(self, obj):
                return "%s by %s" % (obj.name, obj.author.name)

        author = Author.objects.create(name="Author")
        self.book.author = author
        resource = B()
        full_title = resource.export_field(resource.get_fields()[0], self.book)
        self.assertEqual(
            full_title, "%s by %s" % (self.book.name, self.book.author.name)
        )

    def test_dehydrate_field_using_custom_dehydrate_field_method(self):
        class B(resources.ModelResource):
            full_title = fields.Field(
                column_name="Full title", dehydrate_method="foo_dehydrate_full_title"
            )

            class Meta:
                model = Book
                fields = "full_title"

            def foo_dehydrate_full_title(self, obj):
                return f"{obj.name} by {obj.author.name}"

        author = Author.objects.create(name="Author")
        self.book.author = author
        resource = B()

        full_title = resource.export_field(resource.get_fields()[0], self.book)
        self.assertEqual(full_title, f"{self.book.name} by {self.book.author.name}")

    def test_invalid_relation_field_name(self):
        class B(resources.ModelResource):
            full_title = fields.Field(column_name="Full title")

            class Meta:
                model = Book
                # author_name is not a valid field or relation,
                # so should be ignored
                fields = ("author_name", "full_title")

        resource = B()
        self.assertEqual(1, len(resource.fields))
        self.assertEqual("full_title", list(resource.fields.keys())[0])

    def test_widget_format_in_fk_field(self):
        class B(resources.ModelResource):
            class Meta:
                model = Book
                fields = ("author__birthday",)
                widgets = {
                    "author__birthday": {"format": "%Y-%m-%d"},
                }

        author = Author.objects.create(name="Author")
        self.book.author = author
        resource = B()
        result = resource.fields["author__birthday"].export(self.book)
        self.assertEqual(result, str(date.today()))

    def test_widget_kwargs_for_field(self):
        class B(resources.ModelResource):
            class Meta:
                model = Book
                fields = ("published",)
                widgets = {
                    "published": {"format": "%d.%m.%Y"},
                }

        resource = B()
        self.book.published = date(2012, 8, 13)
        result = resource.fields["published"].export(self.book)
        self.assertEqual(result, "13.08.2012")

    def test_foreign_keys_export(self):
        author1 = Author.objects.create(name="Foo")
        self.book.author = author1
        self.book.save()

        dataset = self.resource.export(Book.objects.all())
        self.assertEqual(dataset.dict[0]["author"], author1.pk)

    def test_foreign_keys_import(self):
        author2 = Author.objects.create(name="Bar")
        headers = ["id", "name", "author"]
        row = [None, "FooBook", author2.pk]
        dataset = tablib.Dataset(row, headers=headers)
        self.resource.import_data(dataset, raise_errors=True)

        book = Book.objects.get(name="FooBook")
        self.assertEqual(book.author, author2)

    def test_m2m_export(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        self.book.categories.add(cat1)
        self.book.categories.add(cat2)

        dataset = self.resource.export(Book.objects.all())
        self.assertEqual(dataset.dict[0]["categories"], "%d,%d" % (cat1.pk, cat2.pk))

    def test_m2m_import(self):
        cat1 = Category.objects.create(name="Cat 1")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", str(cat1.pk)]
        dataset = tablib.Dataset(row, headers=headers)
        self.resource.import_data(dataset, raise_errors=True)

        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())

    def test_m2m_options_import(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", "Cat 1|Cat 2"]
        dataset = tablib.Dataset(row, headers=headers)

        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(
                attribute="categories",
                widget=widgets.ManyToManyWidget(Category, field="name", separator="|"),
            )

            class Meta:
                model = Book

        resource = BookM2MResource()
        resource.import_data(dataset, raise_errors=True)
        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())
        self.assertIn(cat2, book.categories.all())

    def test_m2m_add(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        cat3 = Category.objects.create(name="Cat 3")
        cat4 = Category.objects.create(name="Cat 4")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", "Cat 1|Cat 2"]
        dataset = tablib.Dataset(row, headers=headers)

        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(
                attribute="categories",
                m2m_add=True,
                widget=widgets.ManyToManyWidget(Category, field="name", separator="|"),
            )

            class Meta:
                model = Book

        resource = BookM2MResource()
        resource.import_data(dataset, raise_errors=True)
        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())
        self.assertIn(cat2, book.categories.all())
        self.assertNotIn(cat3, book.categories.all())
        self.assertNotIn(cat4, book.categories.all())

        row1 = [
            book.id,
            "FooBook",
            "Cat 1|Cat 2",
        ]  # This should have no effect, since Cat 1 and Cat 2 already exist
        row2 = [book.id, "FooBook", "Cat 3|Cat 4"]
        dataset = tablib.Dataset(row1, row2, headers=headers)
        resource.import_data(dataset, raise_errors=True)
        book2 = Book.objects.get(name="FooBook")
        self.assertEqual(book.id, book2.id)
        self.assertEqual(book.categories.count(), 4)
        self.assertIn(cat1, book2.categories.all())
        self.assertIn(cat2, book2.categories.all())
        self.assertIn(cat3, book2.categories.all())
        self.assertIn(cat4, book2.categories.all())

    def test_related_one_to_one(self):
        # issue #17 - Exception when attempting access something on the
        # related_name

        user = User.objects.create(username="foo")
        profile = Profile.objects.create(user=user)
        Entry.objects.create(user=user)
        Entry.objects.create(user=User.objects.create(username="bar"))

        class EntryResource(resources.ModelResource):
            class Meta:
                model = Entry
                fields = ("user__profile", "user__profile__is_private")

        resource = EntryResource()
        dataset = resource.export(Entry.objects.all())
        self.assertEqual(dataset.dict[0]["user__profile"], profile.pk)
        self.assertEqual(dataset.dict[0]["user__profile__is_private"], "1")
        self.assertEqual(dataset.dict[1]["user__profile"], "")
        self.assertEqual(dataset.dict[1]["user__profile__is_private"], "")

    def test_empty_get_queryset(self):
        # issue #25 - Overriding queryset on export() fails when passed
        # queryset has zero elements
        dataset = self.resource.export(Book.objects.none())
        self.assertEqual(len(dataset), 0)

    def test_import_data_skip_unchanged(self):
        def attempted_save(instance, new, using_transactions, real_dry_run):
            self.fail("Resource attempted to save instead of skipping")

        # Make sure we test with ManyToMany related objects
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        self.book.categories.add(cat1)
        self.book.categories.add(cat2)
        dataset = self.resource.export()

        # Create a new resource that attempts to reimport the data currently
        # in the database while skipping unchanged rows (i.e. all of them)
        resource = deepcopy(self.resource)
        resource._meta.skip_unchanged = True
        # Fail the test if the resource attempts to save the row
        resource.save_instance = attempted_save
        result = resource.import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), len(dataset))
        self.assertTrue(result.rows[0].diff)
        self.assertEqual(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_SKIP)
        self.assertEqual(result.rows[0].object_id, self.book.pk)

        # Test that we can suppress reporting of skipped rows
        resource._meta.report_skipped = False
        result = resource.import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 0)

    def test_before_import_access_to_kwargs(self):
        class B(BookResource):
            def before_import(self, dataset, using_transactions, dry_run, **kwargs):
                if "extra_arg" in kwargs:
                    dataset.headers[dataset.headers.index("author_email")] = "old_email"
                    dataset.insert_col(
                        0, lambda row: kwargs["extra_arg"], header="author_email"
                    )

        resource = B()
        result = resource.import_data(
            self.dataset, raise_errors=True, extra_arg="extra@example.com"
        )
        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 1)
        instance = Book.objects.get(pk=self.book.pk)
        self.assertEqual(instance.author_email, "extra@example.com")

    def test_before_import_raises_error(self):
        class B(BookResource):
            def before_import(self, dataset, using_transactions, dry_run, **kwargs):
                raise Exception("This is an invalid dataset")

        resource = B()
        with self.assertRaises(Exception) as cm:
            resource.import_data(self.dataset, raise_errors=True)
        self.assertEqual("This is an invalid dataset", cm.exception.args[0])

    def test_after_import_raises_error(self):
        class B(BookResource):
            def after_import(
                self, dataset, result, using_transactions, dry_run, **kwargs
            ):
                raise Exception("This is an invalid dataset")

        resource = B()
        with self.assertRaises(Exception) as cm:
            resource.import_data(self.dataset, raise_errors=True)
        self.assertEqual("This is an invalid dataset", cm.exception.args[0])

    def test_link_to_nonexistent_field(self):
        with self.assertRaises(FieldDoesNotExist) as cm:

            class BrokenBook1(resources.ModelResource):
                class Meta:
                    model = Book
                    fields = ("nonexistent__invalid",)

        self.assertEqual(
            "Book.nonexistent: Book has no field named 'nonexistent'",
            cm.exception.args[0],
        )

        with self.assertRaises(FieldDoesNotExist) as cm:

            class BrokenBook2(resources.ModelResource):
                class Meta:
                    model = Book
                    fields = ("author__nonexistent",)

        self.assertEqual(
            "Book.author.nonexistent: Author has no field named " "'nonexistent'",
            cm.exception.args[0],
        )

    def test_link_to_nonrelation_field(self):
        with self.assertRaises(KeyError) as cm:

            class BrokenBook1(resources.ModelResource):
                class Meta:
                    model = Book
                    fields = ("published__invalid",)

        self.assertEqual("Book.published is not a relation", cm.exception.args[0])

        with self.assertRaises(KeyError) as cm:

            class BrokenBook2(resources.ModelResource):
                class Meta:
                    model = Book
                    fields = ("author__name__invalid",)

        self.assertEqual("Book.author.name is not a relation", cm.exception.args[0])

    def test_override_field_construction_in_resource(self):
        class B(resources.ModelResource):
            class Meta:
                model = Book
                fields = ("published",)

            @classmethod
            def field_from_django_field(self, field_name, django_field, readonly):
                if field_name == "published":
                    return {"sound": "quack"}

        B()
        self.assertEqual({"sound": "quack"}, B.fields["published"])

    def test_readonly_annotated_field_import_and_export(self):
        class B(BookResource):
            total_categories = fields.Field("total_categories", readonly=True)

            class Meta:
                model = Book
                skip_unchanged = True

        cat1 = Category.objects.create(name="Cat 1")
        self.book.categories.add(cat1)

        resource = B()

        # Verify that the annotated field is correctly exported
        dataset = resource.export(
            Book.objects.annotate(total_categories=Count("categories"))
        )
        self.assertEqual(int(dataset.dict[0]["total_categories"]), 1)

        # Verify that importing the annotated field raises no errors and that
        # the rows are skipped
        result = resource.import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), len(dataset))
        self.assertEqual(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_SKIP)

    def test_follow_relationship_for_modelresource(self):
        class EntryResource(resources.ModelResource):
            username = fields.Field(attribute="user__username", readonly=False)

            class Meta:
                model = Entry
                fields = ("id",)

            def after_save_instance(self, instance, using_transactions, dry_run):
                if not using_transactions and dry_run:
                    # we don't have transactions and we want to do a dry_run
                    pass
                else:
                    instance.user.save()

        user = User.objects.create(username="foo")
        entry = Entry.objects.create(user=user)
        row = [
            entry.pk,
            "bar",
        ]
        self.dataset = tablib.Dataset(headers=["id", "username"])
        self.dataset.append(row)
        result = EntryResource().import_data(
            self.dataset, raise_errors=True, dry_run=False
        )
        self.assertFalse(result.has_errors())
        self.assertEqual(User.objects.get(pk=user.pk).username, "bar")

    def test_import_data_dynamic_default_callable(self):
        class DynamicDefaultResource(resources.ModelResource):
            class Meta:
                model = WithDynamicDefault
                fields = (
                    "id",
                    "name",
                )

        self.assertTrue(callable(DynamicDefaultResource.fields["name"].default))

        resource = DynamicDefaultResource()
        dataset = tablib.Dataset(
            headers=[
                "id",
                "name",
            ]
        )
        dataset.append([1, None])
        dataset.append([2, None])
        resource.import_data(dataset, raise_errors=False)
        objs = WithDynamicDefault.objects.all()
        self.assertNotEqual(objs[0].name, objs[1].name)

    def test_float_field(self):
        # 433
        class R(resources.ModelResource):
            class Meta:
                model = WithFloatField

        resource = R()
        dataset = tablib.Dataset(
            headers=[
                "id",
                "f",
            ]
        )
        dataset.append([None, None])
        dataset.append([None, ""])
        resource.import_data(dataset, raise_errors=True)
        self.assertEqual(WithFloatField.objects.all()[0].f, None)
        self.assertEqual(WithFloatField.objects.all()[1].f, None)

    def test_get_db_connection_name(self):
        class BookResource(resources.ModelResource):
            class Meta:
                using_db = "other_db"

        self.assertEqual(BookResource().get_db_connection_name(), "other_db")
        self.assertEqual(CategoryResource().get_db_connection_name(), "default")

    def test_import_data_raises_field_for_wrong_db(self):
        class BookResource(resources.ModelResource):
            class Meta:
                using_db = "wrong_db"

        with self.assertRaises(ConnectionDoesNotExist):
            BookResource().import_data(self.dataset)

    def test_natural_foreign_key_detection(self):
        """
        Test that when the _meta option for use_natural_foreign_keys
        is set on a resource that foreign key widgets are created
        with that flag, and when it's off they are not.
        """

        # For future proof testing, we have one resource with natural
        # foreign keys on, and one off. If the default ever changes
        # this should still work.
        class _BookResource_Unfk(resources.ModelResource):
            class Meta:
                use_natural_foreign_keys = True
                model = Book

        class _BookResource(resources.ModelResource):
            class Meta:
                use_natural_foreign_keys = False
                model = Book

        resource_with_nfks = _BookResource_Unfk()
        author_field_widget = resource_with_nfks.fields["author"].widget
        self.assertTrue(author_field_widget.use_natural_foreign_keys)

        resource_without_nfks = _BookResource()
        author_field_widget = resource_without_nfks.fields["author"].widget
        self.assertFalse(author_field_widget.use_natural_foreign_keys)

    def test_natural_foreign_key_false_positives(self):
        """
        Ensure that if the field's model does not have natural foreign
        key functions, it is not set to use natural foreign keys.
        """
        from django.db import models

        class RelatedModel(models.Model):
            name = models.CharField()

            class Meta:
                app_label = "Test"

        class TestModel(models.Model):
            related_field = models.ForeignKey(RelatedModel, on_delete=models.PROTECT)

            class Meta:
                app_label = "Test"

        class TestModelResource(resources.ModelResource):
            class Meta:
                model = TestModel
                fields = ("id", "related_field")
                use_natural_foreign_keys = True

        resource = TestModelResource()
        related_field_widget = resource.fields["related_field"].widget
        self.assertFalse(related_field_widget.use_natural_foreign_keys)