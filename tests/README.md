# Radio Monitoring System API Tests

This directory contains comprehensive API tests for the Radio Monitoring System using pytest and FastAPI's TestClient.

## Test Structure

### Test Categories

- **Authentication Tests** (`test_auth.py`) - PKI certificate authentication, login/logout workflows
- **User Management Tests** (`test_users.py`) - CRUD operations for users, permissions testing  
- **Post Management Tests** (`test_posts.py`) - Monitoring post creation, updates, filtering
- **Observation Tests** (`test_observations.py`) - Signal observation recording, status changes
- **Health Check Tests** (`test_health.py`) - System health and database connectivity
- **Integration Tests** (`test_integration.py`) - Complete user workflows and cross-feature testing
- **Error Scenario Tests** (`test_error_scenarios.py`) - Edge cases, malformed data, security testing

### Test Markers

- `@pytest.mark.auth` - Authentication-specific tests
- `@pytest.mark.api` - General API endpoint tests
- `@pytest.mark.integration` - Integration and workflow tests
- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.slow` - Slower running tests

## Setup

### Database Configuration

Tests use a separate MariaDB test database (`radio_monitoring_test`) that is automatically created and cleaned up.

**Important**: Ensure your MariaDB user has permissions to create/drop databases:

```sql
GRANT ALL PRIVILEGES ON `radio_monitoring_test`.* TO 'dbuser'@'localhost';
GRANT CREATE, DROP ON *.* TO 'dbuser'@'localhost';
```

### Install Dependencies

```bash
pip install -r tests/requirements.txt
```

## Running Tests

### Basic Usage

```bash
# Run all tests
python run_tests.py

# Run specific categories
python run_tests.py --auth           # Authentication tests only
python run_tests.py --api            # API endpoint tests only  
python run_tests.py --integration    # Integration tests only

# Run with coverage
python run_tests.py --coverage

# Generate HTML report
python run_tests.py --html

# Verbose output
python run_tests.py --verbose

# Parallel execution
python run_tests.py --parallel 4
```

### Advanced Usage

```bash
# Run specific test file
python run_tests.py --file tests/test_auth.py

# Run tests with specific markers
python run_tests.py --markers "auth and not slow"

# Using pytest directly
pytest tests/ -v --tb=short
pytest -m auth tests/
pytest tests/test_users.py::TestUserEndpoints::test_create_user_success
```

## Test Features

### Automatic Admin User Creation

Tests automatically create an admin user with:
- Generated PKI certificates
- All system permissions
- Valid authentication tokens

### PKI Certificate Generation  

Real PKI certificates are generated for testing using the `cryptography` library:
- Valid X.509 certificates with proper CN, serial, fingerprint
- Unique certificates per test user
- Proper certificate validation

### Database Isolation

Each test uses transaction rollback for fast cleanup:
- Tests run in isolated database transactions
- No test data persists between tests
- Fast execution with proper cleanup

### Comprehensive Error Testing

Error scenario tests include:
- Malformed JSON payloads
- SQL injection attempts  
- XSS prevention validation
- Concurrent operation testing
- Invalid authentication tokens
- Database constraint violations

## Test Data

### Fixtures Available

- `admin_user` - Admin user with all permissions
- `regular_user` - Regular user with basic permissions  
- `test_post` - Sample monitoring post
- `test_signal_type` - Sample signal type with category
- `admin_auth_headers` - Authorization headers for admin
- `user_auth_headers` - Authorization headers for regular user
- `pki_credentials` - Generated PKI certificate data

### API Route Testing

Tests use the route enum system with hard API versioning:

```python
# Correct pattern used in tests
url = f"{APIVersionRoutes.V1.value}{AuthRoutes.LOGIN.path()}"
# Results in: "/api/v1/auth/login"
```

## Test Coverage

Expected coverage areas:
- ✅ Authentication flows (login, token validation)
- ✅ User CRUD operations with permissions
- ✅ Post management with validation
- ✅ Observation recording and status changes
- ✅ Health check functionality
- ✅ Error handling and edge cases
- ✅ Integration workflows
- ✅ Security validation (XSS, SQL injection prevention)

## Troubleshooting

### Common Issues

1. **Database Connection Errors**
   - Verify MariaDB is running
   - Check database credentials in test configuration
   - Ensure test database creation permissions

2. **PKI Certificate Generation Failures**
   - Check `cryptography` library installation
   - Verify certificate directory permissions

3. **Test Timeouts**
   - Increase pytest timeout for slow tests
   - Check database connection performance
   - Consider using `--parallel` for faster execution

4. **Permission Test Failures**
   - Verify role and permission seeding
   - Check permission enum definitions
   - Validate user role assignments

### Debug Mode

For detailed debugging:

```bash
python run_tests.py --verbose --markers "not slow"
pytest tests/ -v -s --tb=long --log-cli-level=DEBUG
```